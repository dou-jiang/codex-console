"""计划注册任务调度器。"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from ..database import crud
from ..database.session import get_db
from .routes.registration import dispatch_registration_config
from .schedule_utils import compute_next_run_at

logger = logging.getLogger(__name__)


class ScheduledRegistrationService:
    """计划注册任务调度服务。"""

    def __init__(self, poll_interval_seconds: int = 15):
        self.poll_interval_seconds = max(5, poll_interval_seconds)
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self):
        """启动计划任务调度器。"""
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("计划任务调度器已启动，轮询间隔 %s 秒", self.poll_interval_seconds)

    async def stop(self):
        """停止计划任务调度器。"""
        self._running = False
        if not self._task:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("计划任务调度器已停止")

    async def _run_loop(self):
        """执行调度轮询循环。"""
        while self._running:
            try:
                await self.poll_due_jobs()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"计划任务轮询异常: {exc}")
            await asyncio.sleep(self.poll_interval_seconds)

    async def poll_due_jobs(self):
        """扫描并执行到期计划任务。"""
        now = datetime.utcnow()
        with get_db() as db:
            due_jobs = crud.get_due_scheduled_registration_jobs(db, now)
            due_job_uuids = [job.job_uuid for job in due_jobs]
            running_jobs = crud.get_running_scheduled_registration_jobs(db)
            running_job_uuids = [job.job_uuid for job in running_jobs if job.next_run_at and job.next_run_at <= now]

        for job_uuid in running_job_uuids:
            with get_db() as db:
                crud.mark_scheduled_registration_job_skipped(
                    db,
                    job_uuid,
                    "上一次执行尚未结束，已跳过本次触发",
                )

        for job_uuid in due_job_uuids:
            await self.run_job(job_uuid)

    async def run_job(self, job_uuid: str):
        """执行单个计划任务。"""
        now = datetime.utcnow()
        with get_db() as db:
            job = crud.get_scheduled_registration_job_by_uuid(db, job_uuid)
            if not job or not job.enabled:
                return

            if job.is_running:
                crud.mark_scheduled_registration_job_skipped(
                    db,
                    job_uuid,
                    "上一次执行尚未结束，已跳过本次触发",
                )
                return

            next_run_at = compute_next_run_at(
                job.schedule_type,
                job.schedule_config or {},
                now,
                reference_time=job.next_run_at or now,
            )
            claimed_job = crud.claim_scheduled_registration_job(db, job_uuid, next_run_at, now)
            if not claimed_job:
                return
            registration_config = claimed_job.registration_config or {}

        try:
            result = await dispatch_registration_config(registration_config, None)
            with get_db() as db:
                crud.mark_scheduled_registration_job_success(
                    db,
                    job_uuid,
                    datetime.utcnow(),
                    task_uuid=result.get("task_uuid"),
                    batch_id=result.get("batch_id"),
                )
        except Exception as exc:
            logger.warning(f"计划任务执行失败 {job_uuid}: {exc}")
            with get_db() as db:
                crud.mark_scheduled_registration_job_failure(
                    db,
                    job_uuid,
                    str(exc),
                    datetime.utcnow(),
                )


scheduled_registration_service = ScheduledRegistrationService()
