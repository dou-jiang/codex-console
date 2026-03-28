"""
系统自检核心模块
"""

from __future__ import annotations

import importlib.util
import logging
import os
import platform
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Set

from curl_cffi import requests as cffi_requests

from ..config.constants import AccountStatus
from ..config.settings import get_settings
from ..core.dynamic_proxy import get_proxy_url_for_task
from ..core.timezone_utils import to_shanghai_iso
from ..database import crud
from ..database.models import Account, BindCardTask, SelfCheckRun
from ..database.session import get_db

logger = logging.getLogger(__name__)

CHECK_STATUS_PASS = "pass"
CHECK_STATUS_WARN = "warn"
CHECK_STATUS_FAIL = "fail"
CHECK_STATUS_SKIP = "skip"

RUN_STATUS_PENDING = "pending"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_CANCELLED = "cancelled"

PAID_TYPES = {"team", "plus"}
INVALID_ACCOUNT_STATUSES = {
    AccountStatus.FAILED.value,
    AccountStatus.EXPIRED.value,
    AccountStatus.BANNED.value,
}

OVERVIEW_EXTRA_DATA_KEY = "codex_overview"
OVERVIEW_CARD_REMOVED_KEY = "codex_overview_card_removed"

REPAIR_CATALOG: Dict[str, Dict[str, str]] = {
    "repair_team_pool": {
        "name": "清理 Team 池无效账号",
        "description": "将 Team 池中非 plus/team 或无效状态账号移回候选池",
    },
    "repair_clear_overview_cache": {
        "name": "重建账号总览缓存",
        "description": "清除旧总览缓存，触发下次按新逻辑重算",
    },
    "repair_mark_stuck_bind_tasks": {
        "name": "清理超时绑卡任务",
        "description": "将长时间未结束的绑卡任务标记失败，避免队列堆积",
    },
    "repair_fill_orphan_task_email": {
        "name": "补齐孤儿绑卡任务邮箱快照",
        "description": "账号删除后残留任务若缺邮箱快照，则自动补齐文本展示",
    },
    "repair_downgrade_402_to_free": {
        "name": "402 账号订阅降级为 free",
        "description": "将本次自检识别到 HTTP 402 的账号订阅状态改为 free",
    },
}
REPAIR_CENTER_STORE_KEY = "selfcheck.repair_center.store.v1"
REPAIR_CENTER_MAX_ROLLBACKS = 20


def _utc_now() -> datetime:
    return datetime.utcnow()


def _now_iso() -> str:
    return _utc_now().isoformat()


def _parse_dt(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _clamp_int(value: Any, min_value: int, max_value: int, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = int(default)
    return max(min_value, min(max_value, parsed))


def _safe_dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _resolve_selfcheck_proxy_url() -> Optional[str]:
    """
    为系统自检解析代理 URL。
    优先级与业务任务保持一致：代理列表默认项 -> 动态代理/静态代理配置。
    """
    # 1) 代理列表（优先默认代理，否则首个可用代理）
    try:
        with get_db() as db:
            proxy = crud.get_random_proxy(db)
            if proxy and str(proxy.proxy_url or "").strip():
                try:
                    crud.update_proxy_last_used(db, int(proxy.id))
                except Exception:
                    logger.debug("更新自检代理 last_used 失败: proxy_id=%s", getattr(proxy, "id", None), exc_info=True)
                return str(proxy.proxy_url).strip()
    except Exception:
        logger.debug("从代理列表解析自检代理失败", exc_info=True)

    # 2) 动态代理 / 静态代理
    return get_proxy_url_for_task() or get_settings().proxy_url


def _serialize_run(run: SelfCheckRun) -> Dict[str, Any]:
    payload = run.to_dict()
    payload["created_at"] = to_shanghai_iso(run.created_at)
    payload["started_at"] = to_shanghai_iso(run.started_at)
    payload["finished_at"] = to_shanghai_iso(run.finished_at)
    payload["updated_at"] = to_shanghai_iso(run.updated_at)
    return payload


def _build_http_session(proxy_url: Optional[str]) -> cffi_requests.Session:
    kwargs: Dict[str, Any] = {"impersonate": "chrome120"}
    if proxy_url:
        kwargs["proxy"] = proxy_url
    return cffi_requests.Session(**kwargs)


def _probe_endpoint(
    *,
    name: str,
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    timeout_seconds: int = 12,
    expected_codes: Optional[List[int]] = None,
    proxy_url: Optional[str] = None,
    allow_direct_fallback: bool = True,
    critical: bool = False,
) -> Dict[str, Any]:
    expected = set(expected_codes or [200])
    endpoint_result: Dict[str, Any] = {
        "name": name,
        "url": url,
        "method": method.upper(),
        "critical": bool(critical),
        "expected_codes": sorted(expected),
        "ok": False,
        "via": None,
        "http_status": None,
        "error": "",
        "proxy": {"used": bool(proxy_url), "status": None, "error": ""},
        "direct": {"status": None, "error": ""},
    }

    def _request_once(use_proxy: bool) -> Dict[str, Any]:
        session = _build_http_session(proxy_url if use_proxy else None)
        started = time.perf_counter()
        req_kwargs: Dict[str, Any] = {
            "url": url,
            "headers": headers or {},
            "timeout": timeout_seconds,
        }
        if json_body is not None:
            req_kwargs["json"] = json_body
        if method.upper() == "POST":
            resp = session.post(**req_kwargs)
        else:
            resp = session.get(**req_kwargs)
        cost = int((time.perf_counter() - started) * 1000)
        return {"status": int(resp.status_code), "elapsed_ms": cost}

    if proxy_url:
        try:
            proxy_out = _request_once(True)
            endpoint_result["proxy"]["status"] = proxy_out["status"]
            endpoint_result["proxy"]["elapsed_ms"] = proxy_out["elapsed_ms"]
            endpoint_result["via"] = "proxy"
            endpoint_result["http_status"] = proxy_out["status"]
            endpoint_result["ok"] = proxy_out["status"] in expected
            if endpoint_result["ok"]:
                return endpoint_result
            if not allow_direct_fallback:
                endpoint_result["error"] = f"unexpected_status:{proxy_out['status']}"
                return endpoint_result
        except Exception as exc:
            endpoint_result["proxy"]["error"] = str(exc)
            endpoint_result["via"] = "proxy"
            if not allow_direct_fallback:
                endpoint_result["error"] = str(exc)
                return endpoint_result

    try:
        direct_out = _request_once(False)
        endpoint_result["direct"]["status"] = direct_out["status"]
        endpoint_result["direct"]["elapsed_ms"] = direct_out["elapsed_ms"]
        endpoint_result["http_status"] = direct_out["status"]
        endpoint_result["via"] = "direct"
        endpoint_result["ok"] = direct_out["status"] in expected
        if not endpoint_result["ok"]:
            endpoint_result["error"] = f"unexpected_status:{direct_out['status']}"
    except Exception as exc:
        endpoint_result["direct"]["error"] = str(exc)
        endpoint_result["error"] = str(exc)

    return endpoint_result


def _build_check(
    *,
    key: str,
    name: str,
    status: str,
    message: str,
    details: Optional[Dict[str, Any]] = None,
    fixes: Optional[List[str]] = None,
    duration_ms: int = 0,
) -> Dict[str, Any]:
    return {
        "key": key,
        "name": name,
        "status": status,
        "message": message,
        "duration_ms": int(duration_ms or 0),
        "details": details or {},
        "fixes": fixes or [],
    }

def _check_environment() -> Dict[str, Any]:
    started = time.perf_counter()
    warnings: List[str] = []
    failures: List[str] = []

    settings = get_settings()
    tz_name = str(datetime.now().astimezone().tzinfo or "")
    if "shanghai" not in tz_name.lower() and "+08" not in tz_name:
        warnings.append(f"当前进程时区={tz_name}，建议使用 Asia/Shanghai")

    playwright_ready = importlib.util.find_spec("playwright") is not None
    if not playwright_ready:
        warnings.append("未检测到 playwright，涉及本地可视化自动绑卡时会受限")

    cffi_ready = importlib.util.find_spec("curl_cffi") is not None
    if not cffi_ready:
        failures.append("未检测到 curl_cffi，核心网络链路不可用")

    db_url = str(settings.database_url or "")
    db_path = ""
    if db_url.startswith("sqlite:///"):
        db_path = db_url.replace("sqlite:///", "", 1)
    elif db_url and "://" not in db_url:
        db_path = db_url

    if db_path:
        try:
            data_dir = os.path.dirname(os.path.abspath(db_path))
            os.makedirs(data_dir, exist_ok=True)
            with tempfile.NamedTemporaryFile(prefix="selfcheck_", suffix=".tmp", dir=data_dir, delete=True):
                pass
        except Exception as exc:
            failures.append(f"数据库目录写入失败: {exc}")

    logs_dir = os.path.abspath("logs")
    try:
        os.makedirs(logs_dir, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix="selfcheck_", suffix=".tmp", dir=logs_dir, delete=True):
            pass
    except Exception as exc:
        failures.append(f"日志目录写入失败: {exc}")

    if failures:
        status = CHECK_STATUS_FAIL
        message = "环境检查失败：" + "；".join(failures[:2])
    elif warnings:
        status = CHECK_STATUS_WARN
        message = "环境检查通过（存在建议项）"
    else:
        status = CHECK_STATUS_PASS
        message = "环境检查通过"

    return _build_check(
        key="environment",
        name="环境与依赖",
        status=status,
        message=message,
        duration_ms=int((time.perf_counter() - started) * 1000),
        details={
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "timezone": tz_name,
            "playwright_installed": playwright_ready,
            "curl_cffi_installed": cffi_ready,
            "warnings": warnings,
            "failures": failures,
        },
    )


def _check_network(proxy_url: Optional[str]) -> Dict[str, Any]:
    started = time.perf_counter()
    settings = get_settings()
    timeout_seconds = _clamp_int(getattr(settings, "registration_timeout", 120), 5, 30, 12)
    proxy_only_mode = bool(str(proxy_url or "").strip())

    tempmail_base = str(getattr(settings, "tempmail_base_url", "") or "").strip()
    endpoints: List[Dict[str, Any]] = [
        {
            "name": "chatgpt_me",
            "url": "https://chatgpt.com/backend-api/me",
            "method": "GET",
            "expected_codes": [200, 401, 403],
            "critical": True,
        },
        {
            "name": "openai_auth",
            "url": "https://auth.openai.com/",
            "method": "GET",
            "expected_codes": [200, 301, 302, 307, 308, 403] if proxy_only_mode else [200, 301, 302, 307, 308],
            "critical": True,
        },
        {
            "name": "sentinel",
            "url": "https://sentinel.openai.com/backend-api/sentinel/req",
            "method": "POST",
            "json_body": {},
            "expected_codes": [200, 400, 401, 403, 405],
            "critical": False,
        },
    ]
    if tempmail_base:
        endpoints.append(
            {
                "name": "tempmail",
                "url": tempmail_base,
                "method": "GET",
                "expected_codes": [200, 301, 302, 401, 403, 404],
                "critical": False,
            }
        )

    def _run_endpoint(item: Dict[str, Any]) -> Dict[str, Any]:
        return _probe_endpoint(
            name=item["name"],
            url=item["url"],
            method=item.get("method", "GET"),
            json_body=item.get("json_body"),
            expected_codes=item.get("expected_codes"),
            timeout_seconds=timeout_seconds,
            proxy_url=proxy_url,
            allow_direct_fallback=not proxy_only_mode,
            critical=item.get("critical", False),
        )

    checks_indexed: List[Dict[str, Any]] = []
    worker_count = min(4, max(1, len(endpoints)))
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="selfcheck_network") as pool:
        future_map = {pool.submit(_run_endpoint, item): idx for idx, item in enumerate(endpoints)}
        for future in as_completed(future_map):
            idx = future_map[future]
            try:
                checks_indexed.append({"idx": idx, "item": future.result()})
            except Exception as exc:
                item = endpoints[idx]
                checks_indexed.append(
                    {
                        "idx": idx,
                        "item": {
                            "name": item.get("name") or f"endpoint_{idx}",
                            "url": item.get("url") or "",
                            "method": str(item.get("method") or "GET").upper(),
                            "critical": bool(item.get("critical", False)),
                            "expected_codes": item.get("expected_codes") or [200],
                            "ok": False,
                            "via": "proxy" if proxy_only_mode else "direct",
                            "http_status": None,
                            "error": str(exc),
                            "proxy": {"used": bool(proxy_url), "status": None, "error": str(exc) if proxy_only_mode else ""},
                            "direct": {"status": None, "error": str(exc) if not proxy_only_mode else ""},
                        },
                    }
                )
    checks = [row["item"] for row in sorted(checks_indexed, key=lambda row: int(row.get("idx", 0)))]

    critical_failures = [item for item in checks if item["critical"] and not item["ok"]]
    minor_failures = [item for item in checks if (not item["critical"]) and not item["ok"]]

    if critical_failures:
        status = CHECK_STATUS_FAIL
        message = f"关键网络不可达 {len(critical_failures)} 项"
    elif minor_failures:
        status = CHECK_STATUS_WARN
        message = f"网络可用，但有 {len(minor_failures)} 项异常"
    else:
        status = CHECK_STATUS_PASS
        message = "网络连通性正常"

    return _build_check(
        key="network",
        name="网络连通性",
        status=status,
        message=message,
        duration_ms=int((time.perf_counter() - started) * 1000),
        details={
            "proxy_preferred": bool(proxy_url),
            "proxy_mode": "proxy_only" if proxy_only_mode else "direct_or_fallback",
            "checks": checks,
        },
    )


def _probe_account_status(account: Account, fallback_proxy: Optional[str]) -> Dict[str, Any]:
    account_id = int(account.id)
    email = str(account.email or "")
    token = str(account.access_token or "")
    proxy = str(account.proxy_used or "").strip() or fallback_proxy
    if not token:
        return {
            "id": account_id,
            "email": email,
            "http_status": None,
            "state": "missing_token",
            "severity": CHECK_STATUS_WARN,
            "message": "缺少 access_token",
        }

    probe = _probe_endpoint(
        name=f"account_{account_id}",
        url="https://chatgpt.com/backend-api/me",
        method="GET",
        headers={"authorization": f"Bearer {token}", "accept": "application/json"},
        expected_codes=[200, 401, 402, 403],
        timeout_seconds=15,
        proxy_url=proxy,
        critical=False,
    )
    code = probe.get("http_status")
    if code == 200:
        state, severity, message = "ok", CHECK_STATUS_PASS, "可用"
    elif code == 401:
        state, severity, message = "unauthorized", CHECK_STATUS_FAIL, "Token 失效（401）"
    elif code == 402:
        state, severity, message = "payment_required", CHECK_STATUS_WARN, "订阅不可用（402）"
    elif code == 403:
        state, severity, message = "workspace_restricted", CHECK_STATUS_PASS, "工作区受限（403），账号仍可用"
    else:
        state, severity, message = "unknown", CHECK_STATUS_WARN, f"未知状态（{code or 'n/a'}）"

    return {
        "id": account_id,
        "email": email,
        "http_status": code,
        "state": state,
        "severity": severity,
        "message": message,
        "via": probe.get("via"),
    }


def _check_accounts_auth(mode: str, proxy_url: Optional[str]) -> Dict[str, Any]:
    started = time.perf_counter()
    with get_db() as db:
        all_accounts = db.query(Account).order_by(Account.id.desc()).all()

    if not all_accounts:
        return _build_check(
            key="accounts_auth",
            name="账号鉴权抽检",
            status=CHECK_STATUS_WARN,
            message="当前无账号，跳过鉴权抽检",
            duration_ms=int((time.perf_counter() - started) * 1000),
            details={"checked": 0, "total": 0, "accounts": []},
        )

    sample_limit = 8 if mode == "quick" else 40
    sample_accounts = all_accounts[: min(sample_limit, len(all_accounts))]
    details: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(sample_accounts)))) as pool:
        futures = [pool.submit(_probe_account_status, account, proxy_url) for account in sample_accounts]
        for future in as_completed(futures):
            try:
                details.append(future.result())
            except Exception as exc:
                details.append({"id": 0, "email": "-", "http_status": None, "state": "probe_exception", "severity": CHECK_STATUS_WARN, "message": str(exc)})

    details.sort(key=lambda item: int(item.get("id") or 0), reverse=True)
    fail_count = sum(1 for item in details if item.get("severity") == CHECK_STATUS_FAIL)
    warn_count = sum(1 for item in details if item.get("severity") == CHECK_STATUS_WARN)
    pass_count = sum(1 for item in details if item.get("severity") == CHECK_STATUS_PASS)

    fixes: List[str] = []
    if any(int(item.get("http_status") or 0) == 402 for item in details):
        fixes.append("repair_downgrade_402_to_free")
    if fail_count > 0:
        fixes.append("repair_team_pool")

    if fail_count > 0:
        status = CHECK_STATUS_FAIL
        message = f"抽检 {len(details)} 个账号：失败 {fail_count}，警告 {warn_count}"
    elif warn_count > 0:
        status = CHECK_STATUS_WARN
        message = f"抽检 {len(details)} 个账号：警告 {warn_count}"
    else:
        status = CHECK_STATUS_PASS
        message = f"抽检 {len(details)} 个账号全部可用"

    return _build_check(
        key="accounts_auth",
        name="账号鉴权抽检",
        status=status,
        message=message,
        duration_ms=int((time.perf_counter() - started) * 1000),
        fixes=fixes,
        details={
            "checked": len(details),
            "total": len(all_accounts),
            "pass_count": pass_count,
            "warn_count": warn_count,
            "fail_count": fail_count,
            "accounts": details,
        },
    )

def _check_team_pool() -> Dict[str, Any]:
    started = time.perf_counter()
    with get_db() as db:
        rows = db.query(Account).filter(Account.pool_state == "team_pool").all()

    if not rows:
        return _build_check(
            key="team_pool",
            name="Team 池一致性",
            status=CHECK_STATUS_WARN,
            message="当前 Team 池为空",
            duration_ms=int((time.perf_counter() - started) * 1000),
            details={"total": 0, "invalid": 0, "items": []},
        )

    invalid_items: List[Dict[str, Any]] = []
    for account in rows:
        sub = str(account.subscription_type or "").strip().lower()
        status = str(account.status or "").strip().lower()
        invalid_reason = ""
        if sub not in PAID_TYPES:
            invalid_reason = f"subscription={sub or '-'}"
        elif status in INVALID_ACCOUNT_STATUSES:
            invalid_reason = f"status={status}"
        if invalid_reason:
            invalid_items.append({"id": int(account.id), "email": str(account.email or ""), "reason": invalid_reason})

    if invalid_items:
        status = CHECK_STATUS_FAIL
        message = f"Team 池存在 {len(invalid_items)} 个无效账号"
        fixes = ["repair_team_pool"]
    else:
        status = CHECK_STATUS_PASS
        message = "Team 池一致性正常"
        fixes = []

    return _build_check(
        key="team_pool",
        name="Team 池一致性",
        status=status,
        message=message,
        duration_ms=int((time.perf_counter() - started) * 1000),
        fixes=fixes,
        details={"total": len(rows), "invalid": len(invalid_items), "items": invalid_items},
    )


def _check_payment_pipeline() -> Dict[str, Any]:
    started = time.perf_counter()
    now = _utc_now()
    stale_cutoff = now - timedelta(minutes=30)
    stale_statuses = {"link_ready", "opened", "waiting_user_action", "verifying"}

    with get_db() as db:
        stale_tasks = (
            db.query(BindCardTask)
            .filter(BindCardTask.status.in_(list(stale_statuses)), BindCardTask.created_at < stale_cutoff)
            .order_by(BindCardTask.created_at.asc())
            .all()
        )

    playwright_ready = importlib.util.find_spec("playwright") is not None
    stale_count = len(stale_tasks)
    fixes: List[str] = []
    if stale_count > 0:
        fixes.append("repair_mark_stuck_bind_tasks")

    if stale_count >= 8:
        status, message = CHECK_STATUS_FAIL, f"检测到 {stale_count} 个超时绑卡任务"
    elif stale_count > 0 or (not playwright_ready):
        status = CHECK_STATUS_WARN
        if stale_count > 0 and not playwright_ready:
            message = f"存在 {stale_count} 个超时任务，且缺少 playwright"
        elif stale_count > 0:
            message = f"存在 {stale_count} 个超时任务"
        else:
            message = "未检测到 playwright，本地全自动模式受限"
    else:
        status, message = CHECK_STATUS_PASS, "支付链路基础状态正常"

    return _build_check(
        key="payment_pipeline",
        name="支付链路健康",
        status=status,
        message=message,
        duration_ms=int((time.perf_counter() - started) * 1000),
        fixes=fixes,
        details={
            "playwright_installed": playwright_ready,
            "stale_count": stale_count,
            "stale_tasks": [
                {
                    "id": int(task.id),
                    "status": str(task.status or ""),
                    "account_email": str(task.account_email or ""),
                    "created_at": to_shanghai_iso(task.created_at),
                }
                for task in stale_tasks[:50]
            ],
        },
    )


def _check_data_consistency() -> Dict[str, Any]:
    started = time.perf_counter()
    with get_db() as db:
        orphan_tasks = (
            db.query(BindCardTask)
            .filter(BindCardTask.account_id.is_(None), (BindCardTask.account_email.is_(None) | (BindCardTask.account_email == "")))
            .all()
        )
        accounts = db.query(Account).all()

    cached_count = 0
    malformed_count = 0
    for account in accounts:
        extra = _safe_dict(account.extra_data)
        if OVERVIEW_EXTRA_DATA_KEY in extra or OVERVIEW_CARD_REMOVED_KEY in extra:
            cached_count += 1
        elif account.extra_data is not None and not isinstance(account.extra_data, dict):
            malformed_count += 1

    fixes: List[str] = []
    if orphan_tasks:
        fixes.append("repair_fill_orphan_task_email")
    if cached_count > 0:
        fixes.append("repair_clear_overview_cache")

    if orphan_tasks:
        status, message = CHECK_STATUS_FAIL, f"存在 {len(orphan_tasks)} 条缺邮箱快照的孤儿绑卡任务"
    elif malformed_count > 0:
        status, message = CHECK_STATUS_WARN, f"发现 {malformed_count} 条额外数据结构异常"
    else:
        status, message = CHECK_STATUS_PASS, "数据一致性正常"

    return _build_check(
        key="data_consistency",
        name="数据一致性",
        status=status,
        message=message,
        duration_ms=int((time.perf_counter() - started) * 1000),
        fixes=fixes,
        details={
            "orphan_bind_tasks": len(orphan_tasks),
            "cached_overview_accounts": cached_count,
            "malformed_extra_data_accounts": malformed_count,
        },
    )


def _compute_score(checks: List[Dict[str, Any]]) -> Dict[str, int]:
    passed = sum(1 for item in checks if item.get("status") == CHECK_STATUS_PASS)
    warns = sum(1 for item in checks if item.get("status") == CHECK_STATUS_WARN)
    failed = sum(1 for item in checks if item.get("status") == CHECK_STATUS_FAIL)
    total = len(checks)
    score = 0 if total <= 0 else int(round((passed * 100 + warns * 60) / total))
    return {"score": score, "total": total, "passed": passed, "warns": warns, "failed": failed}


def create_selfcheck_run(mode: str = "quick", source: str = "manual") -> Dict[str, Any]:
    mode_text = "full" if str(mode or "").strip().lower() == "full" else "quick"
    source_text = str(source or "manual").strip().lower() or "manual"
    with get_db() as db:
        run = SelfCheckRun(
            run_uuid=str(uuid.uuid4()),
            mode=mode_text,
            source=source_text,
            status=RUN_STATUS_PENDING,
            result_data={"checks": [], "repairs": [], "progress": {"completed": 0, "total": 0}},
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        return _serialize_run(run)


def has_running_selfcheck_run() -> bool:
    with get_db() as db:
        running = db.query(SelfCheckRun).filter(SelfCheckRun.status.in_([RUN_STATUS_PENDING, RUN_STATUS_RUNNING])).count()
    return int(running or 0) > 0


def execute_selfcheck_run(
    run_id: int,
    *,
    mode: Optional[str] = None,
    source: Optional[str] = None,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    cancel_checker: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    with get_db() as db:
        run = db.query(SelfCheckRun).filter(SelfCheckRun.id == int(run_id)).first()
        if not run:
            raise ValueError("自检任务不存在")
        mode_text = "full" if str(mode or run.mode or "").strip().lower() == "full" else "quick"
        source_text = str(source or run.source or "manual").strip().lower() or "manual"
        run.mode = mode_text
        run.source = source_text
        run.status = RUN_STATUS_RUNNING
        run.started_at = _utc_now()
        run.finished_at = None
        run.error_message = None
        run.result_data = {"checks": [], "repairs": [], "progress": {"completed": 0, "total": 0}}
        run.summary = "系统自检运行中"
        db.commit()

    started = time.perf_counter()
    checks: List[Dict[str, Any]] = []
    proxy_url = _resolve_selfcheck_proxy_url()
    check_funcs: List[Callable[[], Dict[str, Any]]] = [_check_environment, lambda: _check_network(proxy_url), lambda: _check_accounts_auth(mode_text, proxy_url)]
    if mode_text == "full":
        check_funcs.extend([_check_team_pool, _check_payment_pipeline, _check_data_consistency])

    total = len(check_funcs)
    failed_error = ""
    try:
        for idx, func in enumerate(check_funcs, start=1):
            if cancel_checker and cancel_checker():
                score_info = _compute_score(checks)
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                with get_db() as db:
                    run = db.query(SelfCheckRun).filter(SelfCheckRun.id == int(run_id)).first()
                    if not run:
                        raise ValueError("自检任务不存在")
                    run.status = RUN_STATUS_CANCELLED
                    run.total_checks = score_info["total"]
                    run.passed_checks = score_info["passed"]
                    run.warning_checks = score_info["warns"]
                    run.failed_checks = score_info["failed"]
                    run.score = score_info["score"]
                    run.duration_ms = elapsed_ms
                    run.summary = f"任务已取消（已完成 {len(checks)}/{total}）"
                    run.error_message = None
                    run.finished_at = _utc_now()
                    data = _safe_dict(run.result_data)
                    data["checks"] = checks
                    data["progress"] = {"completed": len(checks), "total": total}
                    run.result_data = data
                    db.commit()
                    db.refresh(run)
                    return _serialize_run(run)
            item_started = time.perf_counter()
            try:
                item = func()
            except Exception as exc:
                logger.exception("自检项执行异常: run_id=%s index=%s error=%s", run_id, idx, exc)
                item = _build_check(key=f"check_{idx}", name=f"检查项 #{idx}", status=CHECK_STATUS_FAIL, message=f"执行异常: {exc}", duration_ms=int((time.perf_counter() - item_started) * 1000))
            checks.append(item)

            score_info = _compute_score(checks)
            partial_payload = {"checks": checks, "repairs": [], "progress": {"completed": idx, "total": total}}
            with get_db() as db:
                run = db.query(SelfCheckRun).filter(SelfCheckRun.id == int(run_id)).first()
                if run:
                    run.result_data = partial_payload
                    run.total_checks = total
                    run.passed_checks = score_info["passed"]
                    run.warning_checks = score_info["warns"]
                    run.failed_checks = score_info["failed"]
                    run.score = score_info["score"]
                    run.duration_ms = int((time.perf_counter() - started) * 1000)
                    run.updated_at = _utc_now()
                    db.commit()
                    if progress_callback:
                        progress_callback(_serialize_run(run))

        score_info = _compute_score(checks)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        summary = f"完成 {score_info['total']} 项：通过 {score_info['passed']}，警告 {score_info['warns']}，失败 {score_info['failed']}"
        with get_db() as db:
            run = db.query(SelfCheckRun).filter(SelfCheckRun.id == int(run_id)).first()
            if not run:
                raise ValueError("自检任务不存在")
            result_data = _safe_dict(run.result_data)
            result_data["checks"] = checks
            result_data["progress"] = {"completed": total, "total": total}
            run.result_data = result_data
            run.status = RUN_STATUS_COMPLETED if score_info["failed"] == 0 else RUN_STATUS_FAILED
            run.total_checks = score_info["total"]
            run.passed_checks = score_info["passed"]
            run.warning_checks = score_info["warns"]
            run.failed_checks = score_info["failed"]
            run.score = score_info["score"]
            run.duration_ms = elapsed_ms
            run.summary = summary
            run.error_message = None if run.status == RUN_STATUS_COMPLETED else "存在失败检查项"
            run.finished_at = _utc_now()
            db.commit()
            db.refresh(run)
            return _serialize_run(run)
    except Exception as exc:
        failed_error = str(exc)
        logger.exception("自检流程执行失败: run_id=%s error=%s", run_id, exc)

    with get_db() as db:
        run = db.query(SelfCheckRun).filter(SelfCheckRun.id == int(run_id)).first()
        if not run:
            raise ValueError("自检任务不存在")
        run.status = RUN_STATUS_FAILED
        run.error_message = failed_error or "自检流程失败"
        run.finished_at = _utc_now()
        run.duration_ms = int((time.perf_counter() - started) * 1000)
        db.commit()
        db.refresh(run)
        return _serialize_run(run)


def list_selfcheck_runs(limit: int = 20) -> List[Dict[str, Any]]:
    safe_limit = _clamp_int(limit, 1, 200, 20)
    with get_db() as db:
        rows = db.query(SelfCheckRun).order_by(SelfCheckRun.id.desc()).limit(safe_limit).all()
    return [_serialize_run(item) for item in rows]


def get_selfcheck_run(run_id: int) -> Optional[Dict[str, Any]]:
    with get_db() as db:
        run = db.query(SelfCheckRun).filter(SelfCheckRun.id == int(run_id)).first()
    return _serialize_run(run) if run else None

def _repair_team_pool() -> Dict[str, Any]:
    moved = 0
    checked = 0
    with get_db() as db:
        rows = db.query(Account).filter(Account.pool_state == "team_pool").all()
        for account in rows:
            checked += 1
            sub = str(account.subscription_type or "").strip().lower()
            status = str(account.status or "").strip().lower()
            if sub in PAID_TYPES and status not in INVALID_ACCOUNT_STATUSES:
                continue
            account.pool_state = "candidate_pool"
            account.last_pool_sync_at = _utc_now()
            moved += 1
        db.commit()
    return {"checked": checked, "moved_to_candidate_pool": moved}


def _repair_clear_overview_cache() -> Dict[str, Any]:
    affected = 0
    with get_db() as db:
        rows = db.query(Account).all()
        for account in rows:
            extra = _safe_dict(account.extra_data)
            touched = False
            if OVERVIEW_EXTRA_DATA_KEY in extra:
                extra.pop(OVERVIEW_EXTRA_DATA_KEY, None)
                touched = True
            if OVERVIEW_CARD_REMOVED_KEY in extra:
                extra.pop(OVERVIEW_CARD_REMOVED_KEY, None)
                touched = True
            if touched:
                account.extra_data = extra
                affected += 1
        db.commit()
    return {"affected_accounts": affected}


def _repair_mark_stuck_bind_tasks() -> Dict[str, Any]:
    updated = 0
    cutoff = _utc_now() - timedelta(minutes=30)
    stale_statuses = {"link_ready", "opened", "waiting_user_action", "verifying"}
    with get_db() as db:
        rows = db.query(BindCardTask).filter(BindCardTask.status.in_(list(stale_statuses)), BindCardTask.created_at < cutoff).all()
        for task in rows:
            task.status = "failed"
            origin = str(task.last_error or "").strip()
            suffix = "系统自检修复：超时任务自动置为 failed"
            task.last_error = f"{origin} | {suffix}" if origin else suffix
            task.updated_at = _utc_now()
            updated += 1
        db.commit()
    return {"updated_tasks": updated}


def _repair_fill_orphan_task_email() -> Dict[str, Any]:
    fixed = 0
    with get_db() as db:
        rows = db.query(BindCardTask).filter(BindCardTask.account_id.is_(None), (BindCardTask.account_email.is_(None) | (BindCardTask.account_email == ""))).all()
        for task in rows:
            task.account_email = f"deleted-account-{task.id}@history.local"
            task.updated_at = _utc_now()
            fixed += 1
        db.commit()
    return {"fixed_tasks": fixed}


def _collect_402_target_ids(run_id: int) -> List[int]:
    with get_db() as db:
        run = db.query(SelfCheckRun).filter(SelfCheckRun.id == int(run_id)).first()
        if not run:
            return []
        result = _safe_dict(run.result_data)
    checks = result.get("checks") or []
    target_ids: List[int] = []
    for check in checks:
        if str(check.get("key")) != "accounts_auth":
            continue
        details = _safe_dict(check.get("details"))
        for item in details.get("accounts") or []:
            code = int(item.get("http_status") or 0)
            account_id = int(item.get("id") or 0)
            if code == 402 and account_id > 0:
                target_ids.append(account_id)
    return sorted(set(target_ids))


def _load_repair_center_store() -> Dict[str, Any]:
    with get_db() as db:
        setting = crud.get_setting(db, REPAIR_CENTER_STORE_KEY)
        raw = str(getattr(setting, "value", "") or "").strip()
    if not raw:
        return {"rollbacks": []}
    try:
        data = json.loads(raw)
    except Exception:
        return {"rollbacks": []}
    if not isinstance(data, dict):
        return {"rollbacks": []}
    rollbacks = data.get("rollbacks")
    if not isinstance(rollbacks, list):
        rollbacks = []
    return {"rollbacks": rollbacks}


def _save_repair_center_store(store: Dict[str, Any]) -> None:
    payload = json.dumps(store or {"rollbacks": []}, ensure_ascii=False)
    with get_db() as db:
        crud.set_setting(
            db,
            key=REPAIR_CENTER_STORE_KEY,
            value=payload,
            description="系统自检修复中心回滚点",
            category="selfcheck",
        )


def _build_repair_snapshot(run_id: int, repair_keys: List[str]) -> Dict[str, Any]:
    keys = [str(key or "").strip() for key in repair_keys if str(key or "").strip() in REPAIR_CATALOG]
    account_ids: Set[int] = set()
    bind_task_ids: Set[int] = set()

    with get_db() as db:
        if "repair_team_pool" in keys:
            ids = [int(row.id) for row in db.query(Account.id).filter(Account.pool_state == "team_pool").all()]
            account_ids.update(ids)
        if "repair_clear_overview_cache" in keys:
            rows = db.query(Account).all()
            for account in rows:
                extra = _safe_dict(account.extra_data)
                if OVERVIEW_EXTRA_DATA_KEY in extra or OVERVIEW_CARD_REMOVED_KEY in extra:
                    account_ids.add(int(account.id))
        if "repair_downgrade_402_to_free" in keys:
            account_ids.update(_collect_402_target_ids(run_id))
        if "repair_mark_stuck_bind_tasks" in keys:
            cutoff = _utc_now() - timedelta(minutes=30)
            stale_statuses = {"link_ready", "opened", "waiting_user_action", "verifying"}
            ids = [
                int(row.id)
                for row in db.query(BindCardTask.id)
                .filter(BindCardTask.status.in_(list(stale_statuses)), BindCardTask.created_at < cutoff)
                .all()
            ]
            bind_task_ids.update(ids)
        if "repair_fill_orphan_task_email" in keys:
            ids = [
                int(row.id)
                for row in db.query(BindCardTask.id)
                .filter(BindCardTask.account_id.is_(None), (BindCardTask.account_email.is_(None) | (BindCardTask.account_email == "")))
                .all()
            ]
            bind_task_ids.update(ids)

        account_rows = []
        if account_ids:
            rows = db.query(Account).filter(Account.id.in_(list(account_ids))).all()
            for account in rows:
                account_rows.append(
                    {
                        "id": int(account.id),
                        "pool_state": account.pool_state,
                        "pool_state_manual": account.pool_state_manual,
                        "last_pool_sync_at": account.last_pool_sync_at.isoformat() if account.last_pool_sync_at else None,
                        "subscription_type": account.subscription_type,
                        "subscription_at": account.subscription_at.isoformat() if account.subscription_at else None,
                        "extra_data": _safe_dict(account.extra_data),
                    }
                )

        bind_rows = []
        if bind_task_ids:
            rows = db.query(BindCardTask).filter(BindCardTask.id.in_(list(bind_task_ids))).all()
            for task in rows:
                bind_rows.append(
                    {
                        "id": int(task.id),
                        "status": task.status,
                        "last_error": task.last_error,
                        "account_email": task.account_email,
                        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
                    }
                )

    return {
        "accounts": account_rows,
        "bind_card_tasks": bind_rows,
    }


def _append_repair_rollback_entry(entry: Dict[str, Any]) -> None:
    store = _load_repair_center_store()
    rows = list(store.get("rollbacks") or [])
    rows.insert(0, dict(entry or {}))
    store["rollbacks"] = rows[:REPAIR_CENTER_MAX_ROLLBACKS]
    _save_repair_center_store(store)


def _build_preview_item(key: str, run_id: int) -> Dict[str, Any]:
    if key == "repair_team_pool":
        with get_db() as db:
            rows = db.query(Account).filter(Account.pool_state == "team_pool").all()
        impact = 0
        for account in rows:
            sub = str(account.subscription_type or "").strip().lower()
            status = str(account.status or "").strip().lower()
            if sub in PAID_TYPES and status not in INVALID_ACCOUNT_STATUSES:
                continue
            impact += 1
        return {"key": key, "name": REPAIR_CATALOG[key]["name"], "impact_count": impact, "preview": {"checked": len(rows), "will_move": impact}}

    if key == "repair_clear_overview_cache":
        with get_db() as db:
            rows = db.query(Account).all()
        impact = 0
        for account in rows:
            extra = _safe_dict(account.extra_data)
            if OVERVIEW_EXTRA_DATA_KEY in extra or OVERVIEW_CARD_REMOVED_KEY in extra:
                impact += 1
        return {"key": key, "name": REPAIR_CATALOG[key]["name"], "impact_count": impact, "preview": {"affected_accounts": impact}}

    if key == "repair_mark_stuck_bind_tasks":
        cutoff = _utc_now() - timedelta(minutes=30)
        stale_statuses = {"link_ready", "opened", "waiting_user_action", "verifying"}
        with get_db() as db:
            count = db.query(BindCardTask).filter(BindCardTask.status.in_(list(stale_statuses)), BindCardTask.created_at < cutoff).count()
        return {"key": key, "name": REPAIR_CATALOG[key]["name"], "impact_count": int(count or 0), "preview": {"updated_tasks": int(count or 0)}}

    if key == "repair_fill_orphan_task_email":
        with get_db() as db:
            count = db.query(BindCardTask).filter(BindCardTask.account_id.is_(None), (BindCardTask.account_email.is_(None) | (BindCardTask.account_email == ""))).count()
        return {"key": key, "name": REPAIR_CATALOG[key]["name"], "impact_count": int(count or 0), "preview": {"fixed_tasks": int(count or 0)}}

    if key == "repair_downgrade_402_to_free":
        ids = _collect_402_target_ids(run_id)
        return {"key": key, "name": REPAIR_CATALOG[key]["name"], "impact_count": len(ids), "preview": {"matched_402_accounts": len(ids), "account_ids": ids[:200]}}

    return {"key": key, "name": REPAIR_CATALOG.get(key, {}).get("name", key), "impact_count": 0, "preview": {}}


def _repair_downgrade_402_to_free(run_id: int) -> Dict[str, Any]:
    target_ids = _collect_402_target_ids(run_id)
    with get_db() as db:
        if not target_ids:
            return {"updated_accounts": 0, "matched_402_accounts": 0}

        rows = db.query(Account).filter(Account.id.in_(target_ids)).all()
        updated = 0
        for account in rows:
            account.subscription_type = "free"
            account.subscription_at = None
            updated += 1
        db.commit()
        return {"updated_accounts": updated, "matched_402_accounts": len(target_ids)}


def run_repair_action(run_id: int, repair_key: str) -> Dict[str, Any]:
    key = str(repair_key or "").strip()
    if key not in REPAIR_CATALOG:
        raise ValueError("不支持的修复动作")

    started = time.perf_counter()
    if key == "repair_team_pool":
        detail = _repair_team_pool()
    elif key == "repair_clear_overview_cache":
        detail = _repair_clear_overview_cache()
    elif key == "repair_mark_stuck_bind_tasks":
        detail = _repair_mark_stuck_bind_tasks()
    elif key == "repair_fill_orphan_task_email":
        detail = _repair_fill_orphan_task_email()
    elif key == "repair_downgrade_402_to_free":
        detail = _repair_downgrade_402_to_free(run_id)
    else:
        raise ValueError("不支持的修复动作")

    repair_entry = {
        "key": key,
        "name": REPAIR_CATALOG[key]["name"],
        "finished_at": to_shanghai_iso(_utc_now()),
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "detail": detail,
    }

    with get_db() as db:
        run = db.query(SelfCheckRun).filter(SelfCheckRun.id == int(run_id)).first()
        if run:
            data = _safe_dict(run.result_data)
            repairs = list(data.get("repairs") or [])
            repairs.append(repair_entry)
            data["repairs"] = repairs[-200:]
            run.result_data = data
            run.updated_at = _utc_now()
            db.commit()

    return repair_entry


def preview_repair_actions(run_id: int, repair_keys: Optional[List[str]] = None) -> Dict[str, Any]:
    keys = [str(key or "").strip() for key in (repair_keys or list(REPAIR_CATALOG.keys())) if str(key or "").strip() in REPAIR_CATALOG]
    items = [_build_preview_item(key, run_id) for key in keys]
    total_impact = sum(int(item.get("impact_count") or 0) for item in items)
    return {
        "run_id": int(run_id),
        "keys": keys,
        "total_impact": total_impact,
        "items": items,
    }


def list_repair_rollbacks(limit: int = 20) -> List[Dict[str, Any]]:
    safe_limit = _clamp_int(limit, 1, REPAIR_CENTER_MAX_ROLLBACKS, 20)
    store = _load_repair_center_store()
    rows = list(store.get("rollbacks") or [])
    result: List[Dict[str, Any]] = []
    for item in rows[:safe_limit]:
        if not isinstance(item, dict):
            continue
        result.append(
            {
                "rollback_id": item.get("rollback_id"),
                "created_at": item.get("created_at"),
                "run_id": item.get("run_id"),
                "repair_keys": item.get("repair_keys") or [],
                "counts": item.get("counts") or {},
            }
        )
    return result


def execute_repair_plan(run_id: int, repair_keys: List[str], actor: str = "system") -> Dict[str, Any]:
    keys = [str(key or "").strip() for key in (repair_keys or []) if str(key or "").strip() in REPAIR_CATALOG]
    if not keys:
        raise ValueError("修复计划为空")

    preview = preview_repair_actions(run_id, keys)
    rollback_id = str(uuid.uuid4())
    snapshot = _build_repair_snapshot(run_id, keys)
    rollback_entry = {
        "rollback_id": rollback_id,
        "created_at": _now_iso(),
        "run_id": int(run_id),
        "actor": str(actor or "system"),
        "repair_keys": keys,
        "counts": {
            "accounts": len(snapshot.get("accounts") or []),
            "bind_card_tasks": len(snapshot.get("bind_card_tasks") or []),
        },
        "snapshot": snapshot,
    }
    _append_repair_rollback_entry(rollback_entry)

    results: List[Dict[str, Any]] = []
    for key in keys:
        results.append(run_repair_action(run_id, key))

    try:
        with get_db() as db:
            crud.create_operation_audit_log(
                db,
                actor=actor,
                action="selfcheck.repair_center.execute",
                target_type="selfcheck_run",
                target_id=run_id,
                payload={
                    "rollback_id": rollback_id,
                    "repair_keys": keys,
                    "preview_total_impact": int(preview.get("total_impact") or 0),
                },
            )
    except Exception:
        logger.debug("记录修复中心审计日志失败: run_id=%s", run_id, exc_info=True)

    return {
        "run_id": int(run_id),
        "rollback_id": rollback_id,
        "preview": preview,
        "results": results,
    }


def rollback_repair_plan(rollback_id: str) -> Dict[str, Any]:
    rollback_key = str(rollback_id or "").strip()
    if not rollback_key:
        raise ValueError("rollback_id 不能为空")

    store = _load_repair_center_store()
    rollbacks = list(store.get("rollbacks") or [])
    target: Optional[Dict[str, Any]] = None
    for item in rollbacks:
        if isinstance(item, dict) and str(item.get("rollback_id") or "").strip() == rollback_key:
            target = item
            break
    if not target:
        raise ValueError("回滚点不存在")

    snapshot = _safe_dict(target.get("snapshot"))
    account_rows = snapshot.get("accounts") or []
    bind_rows = snapshot.get("bind_card_tasks") or []
    restored_accounts = 0
    restored_bind_tasks = 0

    with get_db() as db:
        if account_rows:
            account_ids = [int(item.get("id") or 0) for item in account_rows if int(item.get("id") or 0) > 0]
            account_map = {int(row.id): row for row in db.query(Account).filter(Account.id.in_(account_ids)).all()}
            for item in account_rows:
                account_id = int(item.get("id") or 0)
                account = account_map.get(account_id)
                if not account:
                    continue
                account.pool_state = item.get("pool_state")
                account.pool_state_manual = item.get("pool_state_manual")
                account.last_pool_sync_at = _parse_dt(item.get("last_pool_sync_at"))
                account.subscription_type = item.get("subscription_type")
                account.subscription_at = _parse_dt(item.get("subscription_at"))
                account.extra_data = _safe_dict(item.get("extra_data"))
                restored_accounts += 1

        if bind_rows:
            bind_ids = [int(item.get("id") or 0) for item in bind_rows if int(item.get("id") or 0) > 0]
            bind_map = {int(row.id): row for row in db.query(BindCardTask).filter(BindCardTask.id.in_(bind_ids)).all()}
            for item in bind_rows:
                bind_id = int(item.get("id") or 0)
                task = bind_map.get(bind_id)
                if not task:
                    continue
                task.status = item.get("status")
                task.last_error = item.get("last_error")
                task.account_email = item.get("account_email")
                task.updated_at = _parse_dt(item.get("updated_at"))
                restored_bind_tasks += 1
        db.commit()

    try:
        with get_db() as db:
            crud.create_operation_audit_log(
                db,
                actor="system",
                action="selfcheck.repair_center.rollback",
                target_type="selfcheck_repair_rollback",
                target_id=rollback_key,
                payload={
                    "restored_accounts": restored_accounts,
                    "restored_bind_card_tasks": restored_bind_tasks,
                },
            )
    except Exception:
        logger.debug("记录修复中心回滚审计日志失败: rollback_id=%s", rollback_key, exc_info=True)

    return {
        "rollback_id": rollback_key,
        "restored_accounts": restored_accounts,
        "restored_bind_card_tasks": restored_bind_tasks,
    }
