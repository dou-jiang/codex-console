"""
账号管理自动一键刷新调度器。

目标：
- 按配置定时执行“批量验证 -> 批量订阅检测”两段流程
- 保持单任务运行，避免并发堆积导致卡顿
- 弱网/异常场景自动退避重试
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from ..config.settings import get_settings

logger = logging.getLogger(__name__)

AUTO_MIN_INTERVAL_MINUTES = 5
AUTO_MAX_INTERVAL_MINUTES = 24 * 60
AUTO_MAX_RETRY_LIMIT = 5
SCHEDULER_POLL_SECONDS = 5
SCHEDULER_BUSY_RETRY_SECONDS = 120
SCHEDULER_FAILURE_BACKOFF_BASE_SECONDS = 30
SCHEDULER_FAILURE_BACKOFF_MAX_SECONDS = 600
SCHEDULER_LOG_MAX_ENTRIES = 100
SCHEDULER_LOG_SNAPSHOT_LIMIT = 40


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _clamp_int(value: Any, min_value: int, max_value: int, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = int(default)
    return max(min_value, min(max_value, parsed))


class AutoQuickRefreshScheduler:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running: bool = False
        self._run_now_requested: bool = False
        self._next_run_at: Optional[datetime] = None
        self._last_started_at: Optional[datetime] = None
        self._last_finished_at: Optional[datetime] = None
        self._last_status: str = "idle"  # idle / running / success / failed / skipped_busy
        self._last_reason: str = ""
        self._last_error: str = ""
        self._last_result: Dict[str, Any] = {}
        self._consecutive_failures: int = 0
        self._logs: List[Dict[str, str]] = []
        self._run_tasks: Set[asyncio.Task] = set()

    def _append_log_locked(self, level: str, message: str, when: Optional[datetime] = None) -> None:
        entry = {
            "time": _to_iso(when or _utc_now()) or "",
            "level": str(level or "info").lower(),
            "message": str(message or "").strip(),
        }
        self._logs.append(entry)
        if len(self._logs) > SCHEDULER_LOG_MAX_ENTRIES:
            del self._logs[0 : len(self._logs) - SCHEDULER_LOG_MAX_ENTRIES]

    def _append_log(self, level: str, message: str, when: Optional[datetime] = None) -> None:
        with self._lock:
            self._append_log_locked(level, message, when=when)

    @staticmethod
    def _build_summary_text(run_result: Dict[str, Any]) -> str:
        summary = (run_result or {}).get("summary") or {}
        validate = summary.get("validate") or {}
        subscription = summary.get("subscription") or {}
        return (
            f"验证 {int(validate.get('valid_count') or 0)}/{int(validate.get('total') or 0)}，"
            f"订阅 {int(subscription.get('success_count') or 0)}/{int(subscription.get('total') or 0)}"
        )

    def _read_schedule(self) -> Dict[str, Any]:
        settings = get_settings()
        enabled = bool(getattr(settings, "auto_quick_refresh_enabled", False))
        interval_minutes = _clamp_int(
            getattr(settings, "auto_quick_refresh_interval_minutes", 30),
            AUTO_MIN_INTERVAL_MINUTES,
            AUTO_MAX_INTERVAL_MINUTES,
            30,
        )
        retry_limit = _clamp_int(
            getattr(settings, "auto_quick_refresh_retry_limit", 2),
            0,
            AUTO_MAX_RETRY_LIMIT,
            2,
        )
        return {
            "enabled": enabled,
            "interval_minutes": interval_minutes,
            "retry_limit": retry_limit,
        }

    def _snapshot_locked(self) -> Dict[str, Any]:
        schedule = self._read_schedule()
        return {
            "enabled": bool(schedule["enabled"]),
            "interval_minutes": int(schedule["interval_minutes"]),
            "retry_limit": int(schedule["retry_limit"]),
            "running": bool(self._running),
            "run_now_requested": bool(self._run_now_requested),
            "next_run_at": _to_iso(self._next_run_at),
            "last_started_at": _to_iso(self._last_started_at),
            "last_finished_at": _to_iso(self._last_finished_at),
            "last_status": self._last_status,
            "last_reason": self._last_reason,
            "last_error": self._last_error,
            "last_result": self._last_result or {},
            "consecutive_failures": int(self._consecutive_failures),
            "logs": list(self._logs[-SCHEDULER_LOG_SNAPSHOT_LIMIT:]),
        }

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()

    def notify_schedule_updated(self) -> Dict[str, Any]:
        now = _utc_now()
        schedule = self._read_schedule()
        with self._lock:
            if not schedule["enabled"] and not self._running:
                self._next_run_at = None
                self._run_now_requested = False
                self._append_log_locked("info", "定时自动一键刷新已禁用", when=now)
            elif schedule["enabled"] and (not self._running):
                self._next_run_at = now + timedelta(minutes=int(schedule["interval_minutes"]))
                self._append_log_locked(
                    "info",
                    f"定时自动一键刷新已启用，每 {int(schedule['interval_minutes'])} 分钟执行",
                    when=now,
                )
            return self._snapshot_locked()

    def request_run_now(self, reason: str = "manual") -> Dict[str, Any]:
        now = _utc_now()
        with self._lock:
            self._run_now_requested = True
            if not self._running:
                self._next_run_at = now
            if reason:
                self._last_reason = str(reason)
            self._append_log_locked("info", "已请求立即执行一次", when=now)
            return self._snapshot_locked()

    def _track_run_task(self, task: asyncio.Task) -> None:
        self._run_tasks.add(task)
        task.add_done_callback(lambda done: self._run_tasks.discard(done))

    async def _cancel_run_tasks(self) -> None:
        pending = [task for task in list(self._run_tasks) if not task.done()]
        if not pending:
            return
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def run_loop(self) -> None:
        logger.info("自动一键刷新调度器启动")
        self._append_log("info", "调度器已启动")
        while True:
            try:
                await self._tick_once()
                await asyncio.sleep(SCHEDULER_POLL_SECONDS)
            except asyncio.CancelledError:
                logger.info("自动一键刷新调度器已停止")
                self._append_log("info", "调度器已停止")
                await self._cancel_run_tasks()
                break
            except Exception as exc:
                logger.warning("自动一键刷新调度器异常: %s", exc)
                await asyncio.sleep(SCHEDULER_POLL_SECONDS)

    async def _tick_once(self) -> None:
        schedule = self._read_schedule()
        now = _utc_now()

        should_start = False
        reason = "scheduled"

        with self._lock:
            if not schedule["enabled"]:
                if not self._running:
                    self._next_run_at = None
                    self._run_now_requested = False
                return

            if self._running:
                return

            if self._next_run_at is None:
                self._next_run_at = now + timedelta(minutes=int(schedule["interval_minutes"]))
                return

            if self._run_now_requested or now >= self._next_run_at:
                should_start = True
                reason = "manual" if self._run_now_requested else "scheduled"
                self._running = True
                self._run_now_requested = False
                self._last_status = "running"
                self._last_reason = reason
                self._last_error = ""
                self._last_result = {}
                self._last_started_at = now
                self._last_finished_at = None
                self._append_log_locked("info", f"开始执行（{reason}）", when=now)

        if should_start:
            task = asyncio.create_task(self._run_once(schedule, reason))
            self._track_run_task(task)

    async def _run_once(self, schedule: Dict[str, Any], reason: str) -> None:
        run_status = "failed"
        run_error = ""
        run_result: Dict[str, Any] = {}

        try:
            run_result, run_status, run_error = await self._execute_with_retry(schedule, reason)
        except Exception as exc:
            run_status = "failed"
            run_error = str(exc)
            run_result = {}

        now = _utc_now()
        interval_minutes = int(schedule["interval_minutes"])

        with self._lock:
            self._running = False
            self._last_finished_at = now
            self._last_status = run_status
            self._last_error = run_error
            self._last_result = run_result or {}

            if run_status == "success":
                self._consecutive_failures = 0
                self._next_run_at = now + timedelta(minutes=interval_minutes)
                self._append_log_locked("success", f"执行完成：{self._build_summary_text(run_result)}", when=now)
            elif run_status == "skipped_busy":
                self._consecutive_failures = 0
                self._next_run_at = now + timedelta(seconds=SCHEDULER_BUSY_RETRY_SECONDS)
                self._append_log_locked("warning", "系统忙，已跳过本次执行并稍后重试", when=now)
            else:
                self._consecutive_failures += 1
                backoff_seconds = min(
                    SCHEDULER_FAILURE_BACKOFF_MAX_SECONDS,
                    SCHEDULER_FAILURE_BACKOFF_BASE_SECONDS * (2 ** max(0, self._consecutive_failures - 1)),
                )
                self._next_run_at = now + timedelta(seconds=backoff_seconds)
                error_text = str(run_error or "unknown_error")
                self._append_log_locked("error", f"执行失败：{error_text}", when=now)

        if run_status == "success":
            logger.info("自动一键刷新完成: reason=%s result=%s", reason, run_result)
        elif run_status == "skipped_busy":
            logger.info("自动一键刷新跳过（系统忙）: reason=%s", reason)
        else:
            logger.warning("自动一键刷新失败: reason=%s error=%s", reason, run_error)

    async def _execute_with_retry(self, schedule: Dict[str, Any], reason: str) -> Tuple[Dict[str, Any], str, str]:
        retry_limit = int(schedule.get("retry_limit") or 0)
        max_attempts = max(1, retry_limit + 1)
        last_error = ""

        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                await asyncio.sleep(min(120, 15 * attempt))

            try:
                result = await asyncio.to_thread(self._execute_once, reason, attempt)
                if result.get("skipped"):
                    return result, "skipped_busy", ""
                return result, "success", ""
            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "自动一键刷新执行失败: reason=%s attempt=%s/%s error=%s",
                    reason,
                    attempt,
                    max_attempts,
                    exc,
                )
                self._append_log(
                    "warning",
                    f"执行重试失败（第 {attempt}/{max_attempts} 次）：{last_error}",
                )

        return {}, "failed", last_error or "unknown_error"

    def _execute_once(self, reason: str, attempt: int) -> Dict[str, Any]:
        from .routes import accounts as accounts_routes

        if accounts_routes.has_active_batch_operations():
            return {"skipped": True, "reason": "busy", "attempt": attempt}

        summary = accounts_routes.run_quick_refresh_workflow(source=f"auto:{reason}")
        return {
            "skipped": False,
            "reason": reason,
            "attempt": attempt,
            "summary": summary,
        }


auto_quick_refresh_scheduler = AutoQuickRefreshScheduler()
