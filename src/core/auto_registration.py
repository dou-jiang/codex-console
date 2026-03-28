import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from ..config.settings import Settings, get_settings
from .upload.cpa_upload import count_ready_cpa_auth_files, list_cpa_auth_files
from ..database import crud
from ..database.session import get_db

logger = logging.getLogger(__name__)
AUTO_REGISTRATION_CHANNEL = "auto-registration"
_auto_registration_state = {
    "enabled": False,
    "status": "idle",
    "message": "自动注册未启动",
    "current_batch_id": None,
    "current_ready_count": None,
    "target_ready_count": None,
    "last_checked_at": None,
    "last_triggered_at": None,
}
_coordinator_instance = None


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _remaining_delay(target_time: float, now: float) -> float:
    return max(0.0, target_time - now)


def update_auto_registration_state(**kwargs) -> dict:
    _auto_registration_state.update(kwargs)
    return get_auto_registration_state()


def get_auto_registration_state() -> dict:
    return dict(_auto_registration_state)


def register_auto_registration_coordinator(
    coordinator: Optional["AutoRegistrationCoordinator"],
) -> None:
    global _coordinator_instance
    _coordinator_instance = coordinator


def trigger_auto_registration_check() -> None:
    coordinator = _coordinator_instance
    if coordinator is not None:
        coordinator.request_immediate_check()


def add_auto_registration_log(message: str) -> None:
    from ..web.task_manager import task_manager

    task_manager.add_batch_log(AUTO_REGISTRATION_CHANNEL, message)


def get_auto_registration_logs() -> list[str]:
    from ..web.task_manager import task_manager

    return task_manager.get_batch_logs(AUTO_REGISTRATION_CHANNEL)


@dataclass
class AutoRegistrationPlan:
    deficit: int
    ready_count: int
    min_ready_auth_files: int
    cpa_service_id: int


def get_auto_registration_inventory(
    settings: Settings,
) -> Optional[tuple[int, int, int]]:
    cpa_service_id = int(settings.registration_auto_cpa_service_id or 0)
    if cpa_service_id <= 0:
        logger.warning("自动注册已启用，但未配置 CPA 服务 ID，跳过库存检查")
        return None

    with get_db() as db:
        cpa_service = crud.get_cpa_service_by_id(db, cpa_service_id)

    if not cpa_service:
        logger.warning("自动注册目标 CPA 服务不存在: %s", cpa_service_id)
        return None

    if not cpa_service.enabled:
        logger.warning("自动注册目标 CPA 服务已禁用: %s", cpa_service.name)
        return None

    success, payload, message = list_cpa_auth_files(
        str(cpa_service.api_url),
        str(cpa_service.api_token),
    )
    if not success:
        logger.warning("自动注册读取 auth-files 库存失败: %s", message)
        return None

    ready_count = count_ready_cpa_auth_files(payload)
    min_ready_auth_files = max(1, int(settings.registration_auto_min_ready_auth_files))
    deficit = max(0, min_ready_auth_files - ready_count)
    return ready_count, min_ready_auth_files, deficit


def build_auto_registration_plan(settings: Settings) -> Optional[AutoRegistrationPlan]:
    if not settings.registration_auto_enabled:
        return None

    cpa_service_id = int(settings.registration_auto_cpa_service_id or 0)
    inventory = get_auto_registration_inventory(settings)
    if inventory is None:
        return None

    ready_count, min_ready_auth_files, deficit = inventory
    if deficit <= 0:
        logger.info(
            "自动注册库存充足，当前可用 %s / 目标 %s",
            ready_count,
            min_ready_auth_files,
        )

    return AutoRegistrationPlan(
        deficit=deficit,
        ready_count=ready_count,
        min_ready_auth_files=min_ready_auth_files,
        cpa_service_id=cpa_service_id,
    )


class AutoRegistrationCoordinator:
    def __init__(
        self,
        trigger_callback: Callable[[AutoRegistrationPlan, Settings], Awaitable[Any]],
        settings_getter: Callable[[], Settings] = get_settings,
        plan_builder: Callable[
            [Settings], Optional[AutoRegistrationPlan]
        ] = build_auto_registration_plan,
    ):
        self._trigger_callback = trigger_callback
        self._settings_getter = settings_getter
        self._plan_builder = plan_builder
        self._task: Optional[asyncio.Task] = None
        self._cycle_lock = asyncio.Lock()
        self._wake_event = asyncio.Event()

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        update_auto_registration_state(
            enabled=bool(self._settings_getter().registration_auto_enabled),
            status="idle",
            message="自动注册协调器已启动",
        )
        self._task = asyncio.create_task(
            self._run_forever(), name="auto-registration-loop"
        )

    async def stop(self) -> None:
        if not self._task:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    def request_immediate_check(self) -> None:
        self._wake_event.set()

    async def run_once(self) -> Optional[AutoRegistrationPlan]:
        if self._cycle_lock.locked():
            logger.info("自动注册上一轮仍在执行，跳过重入检查")
            add_auto_registration_log("[自动注册] 上一轮补货仍在执行，跳过本次重入检查")
            return None

        async with self._cycle_lock:
            settings = self._settings_getter()
            update_auto_registration_state(
                enabled=bool(settings.registration_auto_enabled),
                status="disabled"
                if not settings.registration_auto_enabled
                else "checking",
                message="自动注册已禁用"
                if not settings.registration_auto_enabled
                else "正在检查 auth-files 库存",
                last_checked_at=_timestamp()
                if not settings.registration_auto_enabled
                else None,
                current_batch_id=None
                if not settings.registration_auto_enabled
                else _auto_registration_state.get("current_batch_id"),
                current_ready_count=None
                if not settings.registration_auto_enabled
                else _auto_registration_state.get("current_ready_count"),
            )
            if not settings.registration_auto_enabled:
                return None

            add_auto_registration_log("[自动注册] 开始检查 CPA auth-files 库存")
            plan = await asyncio.to_thread(self._plan_builder, settings)
            if not plan:
                update_auto_registration_state(
                    status="idle",
                    message="检查完成，当前无需补货或配置不可用",
                    last_checked_at=_timestamp(),
                    current_batch_id=None,
                    current_ready_count=None,
                )
                add_auto_registration_log("[自动注册] 检查完成，当前无需补货")
                return None

            if plan.deficit <= 0:
                update_auto_registration_state(
                    status="idle",
                    message=f"检查完成，当前 codex 库存充足 ({plan.ready_count}/{plan.min_ready_auth_files})",
                    current_ready_count=plan.ready_count,
                    target_ready_count=plan.min_ready_auth_files,
                    last_checked_at=_timestamp(),
                    current_batch_id=None,
                )
                add_auto_registration_log(
                    f"[自动注册] 检查完成，当前 codex 库存充足 ({plan.ready_count}/{plan.min_ready_auth_files})"
                )
                return None

            logger.info(
                "自动注册准备补货，当前可用 %s / 目标 %s，计划新增 %s",
                plan.ready_count,
                plan.min_ready_auth_files,
                plan.deficit,
            )
            add_auto_registration_log(
                f"[自动注册] 库存不足，当前可用 {plan.ready_count} / 目标 {plan.min_ready_auth_files}，开始补货 {plan.deficit} 个"
            )
            update_auto_registration_state(
                status="running",
                message="自动补货任务运行中",
                current_ready_count=plan.ready_count,
                target_ready_count=plan.min_ready_auth_files,
                last_checked_at=_timestamp(),
                last_triggered_at=_timestamp(),
            )
            await self._trigger_callback(plan, settings)
            return plan

    async def _run_forever(self) -> None:
        loop = asyncio.get_running_loop()
        next_check_at: Optional[float] = None

        while True:
            settings = self._settings_getter()
            interval = max(5, int(settings.registration_auto_check_interval or 60))
            update_auto_registration_state(
                enabled=bool(settings.registration_auto_enabled),
                target_ready_count=max(
                    1, int(settings.registration_auto_min_ready_auth_files or 1)
                ),
            )

            scheduled_start = (
                next_check_at if next_check_at is not None else loop.time()
            )
            wait_seconds = _remaining_delay(scheduled_start, loop.time())
            if wait_seconds > 0:
                try:
                    await asyncio.wait_for(
                        self._wake_event.wait(), timeout=wait_seconds
                    )
                    self._wake_event.clear()
                except asyncio.TimeoutError:
                    pass
            elif self._wake_event.is_set():
                self._wake_event.clear()

            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("自动注册循环执行失败")
                update_auto_registration_state(
                    status="error",
                    message="自动注册循环执行失败，请查看服务端日志",
                    last_checked_at=_timestamp(),
                )
                add_auto_registration_log(
                    "[自动注册] 自动注册循环执行失败，请检查服务端日志"
                )

            next_check_at = loop.time() + interval
