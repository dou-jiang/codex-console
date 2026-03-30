"""
系统自检 API 路由
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, Optional, List

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ...config.settings import get_settings, update_settings
from ...core.system_selfcheck import (
    REPAIR_CATALOG,
    create_selfcheck_run,
    execute_repair_plan,
    execute_selfcheck_run,
    get_selfcheck_run,
    has_running_selfcheck_run,
    list_repair_rollbacks,
    list_selfcheck_runs,
    preview_repair_actions,
    rollback_repair_plan,
    run_repair_action,
)
from ..task_manager import task_manager
from ..selfcheck_scheduler import selfcheck_scheduler

logger = logging.getLogger(__name__)
router = APIRouter()

_selfcheck_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="selfcheck")


class StartSelfCheckRequest(BaseModel):
    mode: str = Field(default="quick", description="quick/full")
    source: str = Field(default="manual", description="manual/api/scheduler")
    run_async: bool = Field(default=True, description="是否异步执行")


class SelfCheckScheduleRequest(BaseModel):
    enabled: bool = False
    interval_minutes: int = Field(default=15, ge=5, le=24 * 60)
    mode: str = Field(default="quick", description="quick/full")
    run_now: bool = False


class RepairCenterPreviewRequest(BaseModel):
    run_id: int
    repair_keys: Optional[List[str]] = None


class RepairCenterExecuteRequest(BaseModel):
    run_id: int
    repair_keys: List[str]


def _normalize_mode(value: Optional[str]) -> str:
    return "full" if str(value or "").strip().lower() == "full" else "quick"


def _resolve_actor_header(request: Optional[Request]) -> str:
    if request is None:
        return "system"
    for key in ("x-operator", "x-user", "x-username"):
        value = str(request.headers.get(key) or "").strip()
        if value:
            return value[:120]
    return "api"


def _selfcheck_task_id(run_id: int) -> str:
    return f"selfcheck-{int(run_id)}"


def _parse_selfcheck_run_id(task_id: str) -> Optional[int]:
    text = str(task_id or "").strip()
    if not text:
        return None
    if text.startswith("selfcheck-"):
        suffix = text.split("selfcheck-", 1)[1]
        if suffix.isdigit():
            return int(suffix)
    if text.isdigit():
        return int(text)
    return None


def _build_running_run_payload() -> Optional[Dict[str, Any]]:
    runs = list_selfcheck_runs(limit=20)
    for item in runs:
        if str(item.get("status")) in {"pending", "running"}:
            return item
    return None


def _run_selfcheck_async(run_id: int, mode: str, source: str, task_id: str) -> None:
    acquired, running, quota = task_manager.try_acquire_domain_slot("selfcheck", task_id)
    if not acquired:
        reason = f"并发配额已满（running={running}, quota={quota}）"
        task_manager.update_domain_task(
            "selfcheck",
            task_id,
            status="failed",
            finished_at=datetime.utcnow().isoformat(),
            message=reason,
            error=reason,
        )
        run = get_selfcheck_run(run_id)
        if run and str(run.get("status") or "").lower() in {"pending", "running"}:
            try:
                # 触发一次受控执行，立即命中取消并写入终态，避免 pending 脏状态。
                task_manager.request_domain_task_cancel("selfcheck", task_id)
                execute_selfcheck_run(
                    run_id,
                    mode=mode,
                    source=source,
                    cancel_checker=lambda: True,
                )
            except Exception:
                logger.debug("自检任务并发拒绝后写入终态失败: run_id=%s", run_id, exc_info=True)
        return
    try:
        task_manager.update_domain_task(
            "selfcheck",
            task_id,
            status="running",
            started_at=datetime.utcnow().isoformat(),
            message="系统自检执行中",
        )
        result = execute_selfcheck_run(
            run_id,
            mode=mode,
            source=source,
            cancel_checker=lambda: task_manager.is_domain_task_cancel_requested("selfcheck", task_id),
        )
        status_text = str(result.get("status") or "").strip().lower()
        mapped_status = status_text if status_text in {"completed", "failed", "cancelled"} else "completed"
        task_manager.update_domain_task(
            "selfcheck",
            task_id,
            status=mapped_status,
            finished_at=datetime.utcnow().isoformat(),
            message=str(result.get("summary") or "系统自检执行完成"),
            error=result.get("error_message"),
            result=result,
            progress={"completed": int(result.get("total_checks") or 0), "total": int(result.get("total_checks") or 0)},
        )
    except Exception as exc:
        logger.exception("系统自检后台执行失败: run_id=%s error=%s", run_id, exc)
        task_manager.update_domain_task(
            "selfcheck",
            task_id,
            status="failed",
            finished_at=datetime.utcnow().isoformat(),
            message=f"任务异常: {exc}",
            error=str(exc),
        )
    finally:
        task_manager.release_domain_slot("selfcheck", task_id)


def _start_selfcheck_background(mode: str, source: str) -> Dict[str, Any]:
    run = create_selfcheck_run(mode=mode, source=source)
    run_id = int(run["id"])
    task_id = _selfcheck_task_id(run_id)
    task_manager.register_domain_task(
        domain="selfcheck",
        task_id=task_id,
        task_type="selfcheck_run",
        payload={"run_id": run_id, "mode": mode, "source": source},
        progress={"completed": 0, "total": 0},
        max_retries=3,
    )
    _selfcheck_executor.submit(_run_selfcheck_async, run_id, mode, source, task_id)
    return get_selfcheck_run(run_id) or run


@router.get("/runs")
def api_list_selfcheck_runs(limit: int = 20):
    return {"runs": list_selfcheck_runs(limit=limit)}


@router.get("/runs/{run_id}")
def api_get_selfcheck_run(run_id: int):
    run = get_selfcheck_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="自检任务不存在")
    return run


@router.post("/runs/{run_id}/cancel")
def api_cancel_selfcheck_run(run_id: int):
    run = get_selfcheck_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="自检任务不存在")
    task_id = _selfcheck_task_id(run_id)
    task_manager.request_domain_task_cancel("selfcheck", task_id)
    return {
        "success": True,
        "task_id": task_id,
        "run_id": run_id,
        "status": "cancelling",
    }


def cancel_selfcheck_domain_task(task_id: str) -> Dict[str, Any]:
    run_id = _parse_selfcheck_run_id(task_id)
    if not run_id:
        raise HTTPException(status_code=404, detail="自检任务不存在")
    task_manager.request_domain_task_cancel("selfcheck", _selfcheck_task_id(run_id))
    return {"success": True, "task_id": _selfcheck_task_id(run_id), "run_id": run_id, "status": "cancelling"}


def retry_selfcheck_domain_task(task_id: str) -> Dict[str, Any]:
    snapshot = task_manager.get_domain_task("selfcheck", task_id)
    payload = dict((snapshot or {}).get("payload") or {})
    run_id = int(payload.get("run_id") or (_parse_selfcheck_run_id(task_id) or 0))
    mode = _normalize_mode(payload.get("mode") or "quick")
    source = str(payload.get("source") or "manual").strip().lower() or "manual"
    if run_id <= 0 and not snapshot:
        raise HTTPException(status_code=404, detail="自检任务不存在")
    latest = _start_selfcheck_background(mode, source)
    return {
        "success": True,
        "message": "已创建新的自检重试任务",
        "run": latest,
        "retry_from": task_id,
    }


@router.post("/runs")
def api_start_selfcheck_run(request: StartSelfCheckRequest):
    mode = _normalize_mode(request.mode)
    source = str(request.source or "manual").strip().lower() or "manual"

    if has_running_selfcheck_run():
        running = _build_running_run_payload()
        raise HTTPException(
            status_code=409,
            detail={
                "message": "已有运行中的自检任务，请稍后再试",
                "running_run": running,
            },
        )

    if request.run_async:
        latest = _start_selfcheck_background(mode, source)
        return {
            "success": True,
            "message": "自检任务已创建并开始执行",
            "run": latest,
        }

    run = create_selfcheck_run(mode=mode, source=source)
    run_id = int(run["id"])
    task_id = _selfcheck_task_id(run_id)
    task_manager.register_domain_task(
        domain="selfcheck",
        task_id=task_id,
        task_type="selfcheck_run",
        payload={"run_id": run_id, "mode": mode, "source": source},
        progress={"completed": 0, "total": 0},
        max_retries=3,
    )
    acquired, running, quota = task_manager.try_acquire_domain_slot("selfcheck", task_id)
    if not acquired:
        reason = f"并发配额已满（running={running}, quota={quota}）"
        task_manager.update_domain_task(
            "selfcheck",
            task_id,
            status="failed",
            finished_at=datetime.utcnow().isoformat(),
            message=reason,
            error=reason,
        )
        raise HTTPException(status_code=429, detail=reason)
    try:
        result = execute_selfcheck_run(
            run_id,
            mode=mode,
            source=source,
            cancel_checker=lambda: task_manager.is_domain_task_cancel_requested("selfcheck", task_id),
        )
        status_text = str(result.get("status") or "").strip().lower()
        mapped_status = status_text if status_text in {"completed", "failed", "cancelled"} else "completed"
        task_manager.update_domain_task(
            "selfcheck",
            task_id,
            status=mapped_status,
            finished_at=datetime.utcnow().isoformat(),
            message=str(result.get("summary") or "系统自检执行完成"),
            error=result.get("error_message"),
            result=result,
            progress={"completed": int(result.get("total_checks") or 0), "total": int(result.get("total_checks") or 0)},
        )
    finally:
        task_manager.release_domain_slot("selfcheck", task_id)
    return {
        "success": True,
        "message": "自检任务执行完成",
        "run": result,
    }


@router.post("/runs/{run_id}/repairs/{repair_key}")
def api_run_selfcheck_repair(run_id: int, repair_key: str):
    run = get_selfcheck_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="自检任务不存在")
    try:
        result = run_repair_action(run_id, repair_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("执行自检修复动作失败: run_id=%s repair_key=%s error=%s", run_id, repair_key, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {
        "success": True,
        "repair": result,
        "run": get_selfcheck_run(run_id),
    }


@router.get("/repairs")
def api_list_selfcheck_repairs():
    return {"repairs": REPAIR_CATALOG}


@router.post("/repair-center/preview")
def api_repair_center_preview(request: RepairCenterPreviewRequest):
    run = get_selfcheck_run(int(request.run_id))
    if not run:
        raise HTTPException(status_code=404, detail="自检任务不存在")
    preview = preview_repair_actions(int(request.run_id), request.repair_keys)
    return {"success": True, "preview": preview}


@router.post("/repair-center/execute")
def api_repair_center_execute(request: RepairCenterExecuteRequest, http_request: Request):
    run = get_selfcheck_run(int(request.run_id))
    if not run:
        raise HTTPException(status_code=404, detail="自检任务不存在")
    try:
        result = execute_repair_plan(
            int(request.run_id),
            request.repair_keys,
            actor=_resolve_actor_header(http_request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, "result": result}


@router.get("/repair-center/rollbacks")
def api_repair_center_rollbacks(limit: int = 20):
    return {"success": True, "items": list_repair_rollbacks(limit=limit)}


@router.post("/repair-center/rollbacks/{rollback_id}/rollback")
def api_repair_center_rollback(rollback_id: str):
    try:
        result = rollback_repair_plan(rollback_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, "result": result}


@router.get("/schedule")
def api_get_selfcheck_schedule():
    settings = get_settings()
    return {
        "enabled": bool(getattr(settings, "selfcheck_auto_enabled", False)),
        "interval_minutes": int(getattr(settings, "selfcheck_interval_minutes", 15) or 15),
        "mode": _normalize_mode(getattr(settings, "selfcheck_mode", "quick")),
        "runtime": selfcheck_scheduler.snapshot(),
    }


@router.post("/schedule")
def api_update_selfcheck_schedule(request: SelfCheckScheduleRequest):
    mode = _normalize_mode(request.mode)
    interval_minutes = max(5, min(24 * 60, int(request.interval_minutes or 15)))

    update_settings(
        selfcheck_auto_enabled=bool(request.enabled),
        selfcheck_interval_minutes=interval_minutes,
        selfcheck_mode=mode,
    )

    selfcheck_scheduler.notify_schedule_updated()
    if request.run_now:
        if request.enabled:
            selfcheck_scheduler.request_run_now("manual")
        elif not has_running_selfcheck_run():
            _start_selfcheck_background(mode, "manual")

    return {
        "success": True,
        "message": "自检定时设置已更新",
        "schedule": {
            "enabled": bool(request.enabled),
            "interval_minutes": interval_minutes,
            "mode": mode,
        },
        "runtime": selfcheck_scheduler.snapshot(),
    }


@router.post("/schedule/run-now")
def api_selfcheck_run_now():
    if has_running_selfcheck_run():
        running = _build_running_run_payload()
        raise HTTPException(
            status_code=409,
            detail={
                "message": "已有运行中的自检任务",
                "running_run": running,
            },
        )
    settings = get_settings()
    mode = _normalize_mode(getattr(settings, "selfcheck_mode", "quick"))
    if bool(getattr(settings, "selfcheck_auto_enabled", False)):
        runtime = selfcheck_scheduler.request_run_now("manual")
    else:
        _start_selfcheck_background(mode, "manual")
        runtime = selfcheck_scheduler.snapshot()
    return {
        "success": True,
        "message": "已请求立即执行自检",
        "runtime": runtime,
    }


@router.get("/runtime")
def api_selfcheck_runtime():
    return selfcheck_scheduler.snapshot()
