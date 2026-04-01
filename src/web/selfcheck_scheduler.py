"""
系统自检定时调度器
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

from ..config.settings import get_settings
from ..core.system_selfcheck import create_selfcheck_run, execute_selfcheck_run, has_running_selfcheck_run

logger = logging.getLogger(__name__)

SELFCHECK_MIN_INTERVAL_MINUTES = 5
SELFCHECK_MAX_INTERVAL_MINUTES = 24 * 60
SELFCHECK_POLL_SECONDS = 5
SELFCHECK_BUSY_RETRY_SECONDS = 90
SELFCHECK_FAILURE_BACKOFF_BASE_SECONDS = 30
SELFCHECK_FAILURE_BACKOFF_MAX_SECONDS = 300
SELFCHECK_LOG_MAX_ENTRIES = 120
SELFCHECK_LOG_SNAPSHOT_LIMIT = 40


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


def _normalize_mode(value: Any) -> str:
    return "full" if str(value or "").strip().lower() == "full" else "quick"


class SelfCheckScheduler:
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
        self._last_run: Optional[Dict[str, Any]] = None
        self._consecutive_failures: int = 0
        self._logs: List[Dict[str, str]] = []
        self._run_tasks: Set[asyncio.Task] = set()

    def _read_schedule(self) -> Dict[str, Any]:
        settings = get_settings()
        enabled = bool(getattr(settings, "selfcheck_auto_enabled", False))
        interval_minutes = _clamp_int(
            getattr(settings, "selfcheck_interval_minutes", 15),
            SELFCHECK_MIN_INTERVAL_MINUTES,
            SELFCHECK_MAX_INTERVAL_MINUTES,
            15,
        )
        mode = _normalize_mode(getattr(settings, "selfcheck_mode", "quick"))
        return {
            "enabled": enabled,
            "interval_minutes": interval_minutes,
            "mode": mode,
        }

    def _append_log_locked(self, level: str, message: str, when: Optional[datetime] = None) -> None:
        self._logs.append(
            {
                "time": _to_iso(when or _utc_now()) or "",
                "level": str(level or "info").lower(),
                "message": str(message or "").strip(),
            }
        )
        if len(self._logs) > SELFCHECK_LOG_MAX_ENTRIES:
            del self._logs[0 : len(self._logs) - SELFCHECK_LOG_MAX_ENTRIES]

    def _append_log(self, level: str, message: str, when: Optional[datetime] = None) -> None:
        with self._lock:
            self._append_log_locked(level, message, when)

    def _snapshot_locked(self) -> Dict[str, Any]:
        schedule = self._read_schedule()
        return {
            "enabled": bool(schedule["enabled"]),
            "interval_minutes": int(schedule["interval_minutes"]),
            "mode": str(schedule["mode"]),
            "running": bool(self._running),
            "run_now_requested": bool(self._run_now_requested),
            "next_run_at": _to_iso(self._next_run_at),
            "last_started_at": _to_iso(self._last_started_at),
            "last_finished_at": _to_iso(self._last_finished_at),
            "last_status": self._last_status,
            "last_reason": self._last_reason,
            "last_error": self._last_error,
            "last_run": self._last_run or None,
            "consecutive_failures": int(self._consecutive_failures),
            "logs": list(self._logs[-SELFCHECK_LOG_SNAPSHOT_LIMIT:]),
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
                self._append_log_locked("info", "系统自检定时任务已禁用", now)
            elif schedule["enabled"] and not self._running:
                self._next_run_at = now + timedelta(minutes=int(schedule["interval_minutes"]))
                self._append_log_locked(
                    "info",
                    f"系统自检定时任务已启用，每 {int(schedule['interval_minutes'])} 分钟执行",
                    now,
                )
            return self._snapshot_locked()

    def request_run_now(self, reason: str = "manual") -> Dict[str, Any]:
        now = _utc_now()
        with self._lock:
            self._run_now_requested = True
            if not self._running:
                self._next_run_at = now
            self._last_reason = str(reason or "manual")
            self._append_log_locked("info", "已请求立即执行一次系统自检", now)
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
        logger.info("系统自检调度器启动")
        self._append_log("info", "调度器已启动")
        while True:
            try:
                await self._tick_once()
                await asyncio.sleep(SELFCHECK_POLL_SECONDS)
            except asyncio.CancelledError:
                logger.info("系统自检调度器已停止")
                self._append_log("info", "调度器已停止")
                await self._cancel_run_tasks()
                break
            except Exception as exc:
                logger.warning("系统自检调度器异常: %s", exc)
                await asyncio.sleep(SELFCHECK_POLL_SECONDS)

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
                self._last_started_at = now
                self._last_finished_at = None
                self._append_log_locked("info", f"开始执行系统自检（{reason}）", now)

        if should_start:
            task = asyncio.create_task(self._run_once(schedule, reason))
            self._track_run_task(task)

    async def _run_once(self, schedule: Dict[str, Any], reason: str) -> None:
        mode = _normalize_mode(schedule.get("mode"))
        status = "failed"
        error = ""
        run_payload: Optional[Dict[str, Any]] = None

        try:
            run_payload, status, error = await asyncio.to_thread(self._execute_once, mode, reason)
        except Exception as exc:
            status = "failed"
            error = str(exc)

        now = _utc_now()
        interval_minutes = int(schedule.get("interval_minutes") or 15)
        with self._lock:
            self._running = False
            self._last_finished_at = now
            self._last_status = status
            self._last_error = error
            self._last_run = run_payload

            if status == "success":
                self._consecutive_failures = 0
                self._next_run_at = now + timedelta(minutes=interval_minutes)
                summary = str((run_payload or {}).get("summary") or "执行完成")
                self._append_log_locked("success", f"系统自检完成：{summary}", now)
            elif status == "skipped_busy":
                self._consecutive_failures = 0
                self._next_run_at = now + timedelta(seconds=SELFCHECK_BUSY_RETRY_SECONDS)
                self._append_log_locked("warning", "已有运行中的自检任务，本轮跳过", now)
            else:
                self._consecutive_failures += 1
                backoff_seconds = min(
                    SELFCHECK_FAILURE_BACKOFF_MAX_SECONDS,
                    SELFCHECK_FAILURE_BACKOFF_BASE_SECONDS * (2 ** max(0, self._consecutive_failures - 1)),
                )
                self._next_run_at = now + timedelta(seconds=backoff_seconds)
                self._append_log_locked("error", f"系统自检失败：{error or 'unknown_error'}", now)

    @staticmethod
    def _execute_once(mode: str, reason: str) -> tuple[Optional[Dict[str, Any]], str, str]:
        if has_running_selfcheck_run():
            return None, "skipped_busy", ""

        source = "scheduler" if reason == "scheduled" else "manual"
        run = create_selfcheck_run(mode=mode, source=source)
        run_id = int(run["id"])
        result = execute_selfcheck_run(run_id, mode=mode, source=source)
        if str(result.get("status")) in {"completed"}:
            return result, "success", ""
        return result, "failed", str(result.get("error_message") or "存在失败检查项")


selfcheck_scheduler = SelfCheckScheduler()
