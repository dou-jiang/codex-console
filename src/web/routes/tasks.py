"""
统一任务中心路由（accounts/payment/selfcheck/auto_team）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..task_manager import task_manager
from . import accounts as accounts_routes
from . import payment as payment_routes
from . import selfcheck as selfcheck_routes

router = APIRouter()

SUPPORTED_DOMAINS = ("accounts", "payment", "selfcheck", "auto_team")


class DomainQuotaRequest(BaseModel):
    quota: int = Field(..., ge=1, le=64)


def _normalize_domain(domain: str) -> str:
    text = str(domain or "").strip().lower()
    if text not in SUPPORTED_DOMAINS:
        raise HTTPException(status_code=400, detail=f"domain 仅支持 {', '.join(SUPPORTED_DOMAINS)}")
    return text


def _normalize_status(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text or "unknown"


def _take_recent(items: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    safe_limit = max(1, min(200, int(limit or 50)))
    sorted_items = sorted(
        items,
        key=lambda row: str(row.get("created_at") or ""),
        reverse=True,
    )
    return sorted_items[:safe_limit]


def _count_status(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    result: Dict[str, int] = {}
    for row in rows:
        status = _normalize_status(row.get("status"))
        result[status] = int(result.get(status, 0)) + 1
    return result


@router.get("/summary")
def get_tasks_summary(limit: int = Query(50, ge=1, le=200)):
    now_iso = datetime.utcnow().isoformat()
    domains: Dict[str, Dict[str, Any]] = {}

    for domain in SUPPORTED_DOMAINS:
        rows = task_manager.list_domain_tasks(domain=domain, limit=500)
        recent = _take_recent(rows, limit)
        domains[domain] = {
            "total": len(rows),
            "by_status": _count_status(rows),
            "recent": recent,
        }

    return {
        "success": True,
        "generated_at": now_iso,
        "quotas": task_manager.domain_quota_snapshot(),
        "accounts": domains.get("accounts", {}),
        "payment": domains.get("payment", {}),
        "selfcheck": domains.get("selfcheck", {}),
        "auto_team": domains.get("auto_team", {}),
        "domains": domains,
    }


@router.get("/quotas")
def get_task_domain_quotas():
    return {
        "success": True,
        "quotas": task_manager.domain_quota_snapshot(),
    }


@router.post("/quotas/{domain}")
def update_task_domain_quota(domain: str, request: DomainQuotaRequest):
    domain_key = _normalize_domain(domain)
    quota = task_manager.set_domain_quota(domain_key, request.quota)
    return {
        "success": True,
        "domain": domain_key,
        "quota": quota,
        "snapshot": task_manager.domain_quota_snapshot(),
    }


@router.get("/{domain}/{task_id}")
def get_unified_task(domain: str, task_id: str):
    domain_key = _normalize_domain(domain)
    task = task_manager.get_domain_task(domain_key, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"success": True, "domain": domain_key, "task": task}


@router.post("/{domain}/{task_id}/cancel")
def cancel_unified_task(domain: str, task_id: str):
    domain_key = _normalize_domain(domain)
    if domain_key == "accounts":
        return accounts_routes.cancel_account_async_task(task_id)
    if domain_key == "payment":
        return payment_routes.cancel_payment_op_task(task_id)
    if domain_key == "selfcheck":
        return selfcheck_routes.cancel_selfcheck_domain_task(task_id)

    # auto_team 目前仅支持全局取消标记（协作保留扩展点）
    snapshot = task_manager.request_domain_task_cancel(domain_key, task_id)
    return {
        "success": True,
        "domain": domain_key,
        "task_id": task_id,
        "status": "cancelling",
        "task": snapshot,
    }


@router.post("/{domain}/{task_id}/pause")
def pause_unified_task(domain: str, task_id: str):
    domain_key = _normalize_domain(domain)
    if domain_key == "accounts":
        return accounts_routes.pause_account_async_task(task_id)
    if domain_key == "payment":
        return payment_routes.pause_payment_op_task(task_id)

    snapshot = task_manager.request_domain_task_pause(domain_key, task_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {
        "success": True,
        "domain": domain_key,
        "task_id": task_id,
        "status": "paused",
        "task": snapshot,
    }


@router.post("/{domain}/{task_id}/resume")
def resume_unified_task(domain: str, task_id: str):
    domain_key = _normalize_domain(domain)
    if domain_key == "accounts":
        return accounts_routes.resume_account_async_task(task_id)
    if domain_key == "payment":
        return payment_routes.resume_payment_op_task(task_id)

    snapshot = task_manager.request_domain_task_resume(domain_key, task_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {
        "success": True,
        "domain": domain_key,
        "task_id": task_id,
        "status": "running",
        "task": snapshot,
    }


@router.post("/{domain}/{task_id}/retry")
def retry_unified_task(domain: str, task_id: str):
    domain_key = _normalize_domain(domain)
    if domain_key == "accounts":
        return {"success": True, "domain": domain_key, "task": accounts_routes.retry_account_async_task(task_id)}
    if domain_key == "payment":
        return {"success": True, "domain": domain_key, "task": payment_routes.retry_payment_op_task(task_id)}
    if domain_key == "selfcheck":
        return selfcheck_routes.retry_selfcheck_domain_task(task_id)

    # auto_team 重试：仅记录请求，业务层按后续异步化接入
    snapshot = task_manager.request_domain_task_retry(domain_key, task_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {
        "success": True,
        "domain": domain_key,
        "task_id": task_id,
        "message": "已记录重试请求（等待 auto_team 异步任务接入）",
        "task": snapshot,
    }
