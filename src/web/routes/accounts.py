"""
账号管理 API 路由
"""
import io
import asyncio
import json
import logging
import re
import zipfile
import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, BackgroundTasks, Body
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func

from ...config.constants import AccountStatus
from ...config.settings import get_settings
from ...core.openai.overview import fetch_codex_overview
from ...core.openai.token_refresh import refresh_account_token as do_refresh
from ...core.openai.token_refresh import validate_account_token as do_validate
from ...core.upload.cpa_upload import generate_token_json, batch_upload_to_cpa, upload_to_cpa
from ...core.upload.team_manager_upload import upload_to_team_manager, batch_upload_to_team_manager
from ...core.upload.sub2api_upload import batch_upload_to_sub2api, upload_to_sub2api

from ...core.dynamic_proxy import get_proxy_url_for_task
from ...core.timezone_utils import utcnow_naive
from ...database import crud
from ...database.models import Account
from ...database.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter()

CURRENT_ACCOUNT_SETTING_KEY = "codex.current_account_id"
OVERVIEW_EXTRA_DATA_KEY = "codex_overview"
OVERVIEW_CARD_REMOVED_KEY = "codex_overview_card_removed"
OVERVIEW_CACHE_TTL_SECONDS = 300  # 5 分钟
PAID_SUBSCRIPTION_TYPES = ("plus", "team")
INVALID_ACCOUNT_STATUSES = (
    AccountStatus.FAILED.value,
    AccountStatus.EXPIRED.value,
    AccountStatus.BANNED.value,
)


def _get_proxy(request_proxy: Optional[str] = None) -> Optional[str]:
    """获取代理 URL，策略与注册流程一致：代理列表 → 动态代理 → 静态配置"""
    if request_proxy:
        return request_proxy
    with get_db() as db:
        proxy = crud.get_random_proxy(db)
        if proxy:
            return proxy.proxy_url
    proxy_url = get_proxy_url_for_task()
    if proxy_url:
        return proxy_url
    return get_settings().proxy_url


def _apply_status_filter(query, status: Optional[str]):
    """
    统一状态筛选:
    - failed/invalid 视为“无效账号集合”（failed + expired + banned）
    - 其他值按精确状态筛选
    """
    normalized = (status or "").strip().lower()
    if not normalized:
        return query
    if normalized in {"failed", "invalid"}:
        return query.filter(Account.status.in_(INVALID_ACCOUNT_STATUSES))
    return query.filter(Account.status == normalized)


# ============== Pydantic Models ==============

class AccountResponse(BaseModel):
    """账号响应模型"""
    id: int
    email: str
    password: Optional[str] = None
    client_id: Optional[str] = None
    email_service: str
    account_id: Optional[str] = None
    workspace_id: Optional[str] = None
    device_id: Optional[str] = None
    registered_at: Optional[str] = None
    last_refresh: Optional[str] = None
    expires_at: Optional[str] = None
    status: str
    proxy_used: Optional[str] = None
    cpa_uploaded: bool = False
    cpa_uploaded_at: Optional[str] = None
    subscription_type: Optional[str] = None
    subscription_at: Optional[str] = None
    cookies: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class AccountListResponse(BaseModel):
    """账号列表响应"""
    total: int
    accounts: List[AccountResponse]


class AccountUpdateRequest(BaseModel):
    """账号更新请求"""
    status: Optional[str] = None
    metadata: Optional[dict] = None
    cookies: Optional[str] = None  # 完整 cookie 字符串，用于支付请求
    session_token: Optional[str] = None


class ManualAccountCreateRequest(BaseModel):
    """手动创建账号请求"""
    email: str
    password: str
    email_service: Optional[str] = "manual"
    status: Optional[str] = AccountStatus.ACTIVE.value
    client_id: Optional[str] = None
    account_id: Optional[str] = None
    workspace_id: Optional[str] = None
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    id_token: Optional[str] = None
    session_token: Optional[str] = None
    cookies: Optional[str] = None
    proxy_used: Optional[str] = None
    source: Optional[str] = "manual"
    subscription_type: Optional[str] = None
    metadata: Optional[dict] = None


class AccountImportItem(BaseModel):
    """账号导入项（支持按账号详情字段导入）"""
    email: str
    password: Optional[str] = None
    email_service: Optional[str] = "manual"
    status: Optional[str] = AccountStatus.ACTIVE.value
    client_id: Optional[str] = None
    account_id: Optional[str] = None
    workspace_id: Optional[str] = None
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    id_token: Optional[str] = None
    session_token: Optional[str] = None
    cookies: Optional[str] = None
    proxy_used: Optional[str] = None
    source: Optional[str] = "import"
    subscription_type: Optional[str] = None
    plan_type: Optional[str] = None
    auth_mode: Optional[str] = None
    user_id: Optional[str] = None
    organization_id: Optional[str] = None
    account_name: Optional[str] = None
    account_structure: Optional[str] = None
    tokens: Optional[dict] = None
    quota: Optional[dict] = None
    tags: Optional[Any] = None
    created_at: Optional[Any] = None
    last_used: Optional[Any] = None
    metadata: Optional[dict] = None


class ImportAccountsRequest(BaseModel):
    """批量导入账号请求"""
    accounts: List[dict]
    overwrite: bool = False


class BatchDeleteRequest(BaseModel):
    """批量删除请求"""
    ids: List[int] = []
    select_all: bool = False
    status_filter: Optional[str] = None
    email_service_filter: Optional[str] = None
    search_filter: Optional[str] = None


class BatchUpdateRequest(BaseModel):
    """批量更新请求"""
    ids: List[int]
    status: str


class OverviewRefreshRequest(BaseModel):
    """账号总览刷新请求"""
    ids: List[int] = []
    force: bool = True
    select_all: bool = False
    status_filter: Optional[str] = None
    email_service_filter: Optional[str] = None
    search_filter: Optional[str] = None
    proxy: Optional[str] = None


class OverviewCardDeleteRequest(BaseModel):
    """账号总览卡片删除（仅从卡片移除，不删除账号）"""
    ids: List[int] = []
    select_all: bool = False
    status_filter: Optional[str] = None
    email_service_filter: Optional[str] = None
    search_filter: Optional[str] = None


# ============== Helper Functions ==============

def resolve_account_ids(
    db,
    ids: List[int],
    select_all: bool = False,
    status_filter: Optional[str] = None,
    email_service_filter: Optional[str] = None,
    search_filter: Optional[str] = None,
) -> List[int]:
    """当 select_all=True 时查询全部符合条件的 ID，否则直接返回传入的 ids"""
    if not select_all:
        return ids
    query = db.query(Account.id)
    if status_filter:
        query = _apply_status_filter(query, status_filter)
    if email_service_filter:
        query = query.filter(Account.email_service == email_service_filter)
    if search_filter:
        pattern = f"%{search_filter}%"
        query = query.filter(
            (Account.email.ilike(pattern)) | (Account.account_id.ilike(pattern))
        )
    return [row[0] for row in query.all()]


def account_to_response(account: Account) -> AccountResponse:
    """转换 Account 模型为响应模型"""
    return AccountResponse(
        id=account.id,
        email=account.email,
        password=account.password,
        client_id=account.client_id,
        email_service=account.email_service,
        account_id=account.account_id,
        workspace_id=account.workspace_id,
        device_id=_resolve_account_device_id(account),
        registered_at=account.registered_at.isoformat() if account.registered_at else None,
        last_refresh=account.last_refresh.isoformat() if account.last_refresh else None,
        expires_at=account.expires_at.isoformat() if account.expires_at else None,
        status=account.status,
        proxy_used=account.proxy_used,
        cpa_uploaded=account.cpa_uploaded or False,
        cpa_uploaded_at=account.cpa_uploaded_at.isoformat() if account.cpa_uploaded_at else None,
        subscription_type=account.subscription_type,
        subscription_at=account.subscription_at.isoformat() if account.subscription_at else None,
        cookies=account.cookies,
        created_at=account.created_at.isoformat() if account.created_at else None,
        updated_at=account.updated_at.isoformat() if account.updated_at else None,
    )


def _extract_cookie_value(cookies_text: Optional[str], cookie_name: str) -> str:
    text = str(cookies_text or "")
    if not text:
        return ""
    pattern = re.compile(rf"(?:^|;\s*){re.escape(cookie_name)}=([^;]+)")
    match = pattern.search(text)
    return str(match.group(1) or "").strip() if match else ""


def _extract_session_token_from_cookie_text(cookies_text: Optional[str]) -> str:
    """从完整 cookie 字符串中提取 next-auth session token（兼容分片）。"""
    text = str(cookies_text or "")
    if not text:
        return ""

    direct = re.search(r"(?:^|;\s*)__Secure-next-auth\.session-token=([^;]+)", text)
    if direct:
        return str(direct.group(1) or "").strip()

    parts = re.findall(r"(?:^|;\s*)__Secure-next-auth\.session-token\.(\d+)=([^;]+)", text)
    if not parts:
        return ""

    chunk_map = {}
    for idx, value in parts:
        try:
            chunk_map[int(idx)] = str(value or "")
        except Exception:
            continue
    if not chunk_map:
        return ""

    return "".join(chunk_map[i] for i in sorted(chunk_map.keys()))


def _resolve_account_device_id(account: Account) -> str:
    """
    解析账号 device_id（兼容历史数据）:
    1) account.device_id（若模型未来扩展该字段）
    2) cookies 里的 oai-did
    3) extra_data 中的 device_id/oai_did/oai-device-id
    """
    direct = str(getattr(account, "device_id", "") or "").strip()
    if direct:
        return direct

    did_in_cookie = _extract_cookie_value(getattr(account, "cookies", None), "oai-did")
    if did_in_cookie:
        return did_in_cookie

    extra_data = getattr(account, "extra_data", None)
    if isinstance(extra_data, dict):
        for key in ("device_id", "oai_did", "oai-device-id"):
            value = str(extra_data.get(key) or "").strip()
            if value:
                return value
    return ""


def _resolve_account_session_token(account: Account) -> str:
    """解析账号 session_token（优先 DB 字段，其次 cookies 文本）。"""
    db_token = str(getattr(account, "session_token", "") or "").strip()
    if db_token:
        return db_token
    return _extract_session_token_from_cookie_text(getattr(account, "cookies", None))


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        text = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _normalize_plan_type(raw_plan: Optional[str]) -> str:
    value = (raw_plan or "").strip().lower()
    if not value:
        return "Basic"
    if "team" in value or "enterprise" in value:
        return "Team"
    if "plus" in value:
        return "Plus"
    if "pro" in value:
        return "Pro"
    if "free" in value or "basic" in value:
        return "Basic"
    return value.capitalize()


def _build_unknown_quota() -> dict:
    return {
        "used": None,
        "total": None,
        "remaining": None,
        "percentage": None,
        "reset_at": None,
        "reset_in_text": "-",
        "status": "unknown",
    }


def _fallback_overview(account: Account, error_message: Optional[str] = None, stale: bool = False) -> dict:
    data = {
        "plan_type": _normalize_plan_type(account.subscription_type),
        "plan_source": "db.subscription_type" if account.subscription_type else "default",
        "hourly_quota": _build_unknown_quota(),
        "weekly_quota": _build_unknown_quota(),
        "code_review_quota": _build_unknown_quota(),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "sources": [],
        "stale": stale,
    }
    if error_message:
        data["error"] = error_message
    return data


def _is_overview_cache_stale(cached_overview: Optional[dict]) -> bool:
    if not isinstance(cached_overview, dict):
        return True
    fetched_at = _parse_iso_datetime(cached_overview.get("fetched_at"))
    if not fetched_at:
        return True
    age = datetime.now(timezone.utc) - fetched_at
    return age > timedelta(seconds=OVERVIEW_CACHE_TTL_SECONDS)


def _get_current_account_id(db) -> Optional[int]:
    setting = crud.get_setting(db, CURRENT_ACCOUNT_SETTING_KEY)
    if not setting or not setting.value:
        return None
    try:
        return int(setting.value)
    except (TypeError, ValueError):
        return None


def _set_current_account_id(db, account_id: int):
    crud.set_setting(
        db,
        key=CURRENT_ACCOUNT_SETTING_KEY,
        value=str(account_id),
        description="当前切换中的 Codex 账号 ID",
        category="accounts",
    )


def _is_overview_card_removed(account: Account) -> bool:
    extra_data = account.extra_data if isinstance(account.extra_data, dict) else {}
    return bool(extra_data.get(OVERVIEW_CARD_REMOVED_KEY))


def _set_overview_card_removed(account: Account, removed: bool):
    extra_data = account.extra_data if isinstance(account.extra_data, dict) else {}
    merged = dict(extra_data)
    if removed:
        merged[OVERVIEW_CARD_REMOVED_KEY] = True
    else:
        merged.pop(OVERVIEW_CARD_REMOVED_KEY, None)
    account.extra_data = merged


def _write_current_account_snapshot(account: Account) -> Optional[str]:
    """
    写入当前账号快照文件，便于外部流程读取当前账号令牌。
    """
    try:
        data_dir = Path("data")
        data_dir.mkdir(parents=True, exist_ok=True)
        output_file = data_dir / "current_codex_account.json"
        payload = {
            "id": account.id,
            "email": account.email,
            "plan_type": _normalize_plan_type(account.subscription_type),
            "access_token": account.access_token,
            "refresh_token": account.refresh_token,
            "id_token": account.id_token,
            "session_token": account.session_token,
            "account_id": account.account_id,
            "workspace_id": account.workspace_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        output_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(output_file)
    except Exception as exc:
        logger.warning(f"写入 current_codex_account.json 失败: {exc}")
        return None


def _plan_to_subscription_type(plan_type: Optional[str]) -> Optional[str]:
    key = (plan_type or "").strip().lower()
    if key.startswith("team"):
        return "team"
    if key.startswith("plus"):
        return "plus"
    return None


def _normalize_subscription_input(value: Optional[str]) -> Optional[str]:
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    if raw in ("team", "enterprise"):
        return "team"
    if raw in ("plus", "pro"):
        return "plus"
    if raw in ("free", "basic", "none", "null"):
        return None
    if "team" in raw:
        return "team"
    if "plus" in raw or "pro" in raw:
        return "plus"
    return None


def _is_paid_subscription(value: Optional[str]) -> bool:
    """是否为付费订阅（plus/team）。"""
    normalized = _normalize_subscription_input(value)
    return normalized in PAID_SUBSCRIPTION_TYPES


def _pick_first_text(*values: Any) -> Optional[str]:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _decode_jwt_payload_unverified(token: Optional[str]) -> Dict[str, Any]:
    """
    无签名校验解码 JWT payload，仅用于导入兜底字段提取。
    """
    text = str(token or "").strip()
    if not text or "." not in text:
        return {}
    try:
        parts = text.split(".")
        if len(parts) < 2:
            return {}
        payload_b64 = parts[1]
        padding = "=" * (-len(payload_b64) % 4)
        payload_raw = base64.urlsafe_b64decode((payload_b64 + padding).encode("utf-8"))
        payload = json.loads(payload_raw.decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _get_nested(data: Dict[str, Any], path: List[str]) -> Any:
    cur: Any = data
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _get_account_overview_data(
    db,
    account: Account,
    force_refresh: bool = False,
    proxy: Optional[str] = None,
    allow_network: bool = True,
) -> tuple[dict, bool]:
    updated = False
    extra_data = account.extra_data if isinstance(account.extra_data, dict) else {}
    cached = extra_data.get(OVERVIEW_EXTRA_DATA_KEY) if isinstance(extra_data, dict) else None
    cache_stale = _is_overview_cache_stale(cached)

    if not account.access_token:
        if cached:
            stale_cached = dict(cached)
            stale_cached["stale"] = True
            stale_cached["error"] = "missing_access_token"
            return stale_cached, updated
        return _fallback_overview(account, error_message="missing_access_token"), updated

    if not force_refresh and cached and not cache_stale:
        return cached, updated

    # 首屏卡片列表默认走“缓存优先”模式，避免首次进入被远端配额请求阻塞导致网络异常。
    if not allow_network:
        if cached:
            stale_cached = dict(cached)
            if cache_stale:
                stale_cached["stale"] = True
                stale_cached.setdefault("error", "cache_stale")
            return stale_cached, updated
        return _fallback_overview(account, error_message="cache_miss", stale=True), updated

    try:
        overview = fetch_codex_overview(account, proxy=proxy)
        if cached and not force_refresh:
            for key in ("hourly_quota", "weekly_quota", "code_review_quota"):
                if (
                    isinstance(cached.get(key), dict)
                    and isinstance(overview.get(key), dict)
                    and overview[key].get("status") == "unknown"
                    and cached[key].get("status") == "ok"
                ):
                    overview[key] = cached[key]

        # 用高置信度来源同步本地订阅状态，确保 Plus/Team 判断可复用。
        plan_source = str(overview.get("plan_source") or "")
        trusted_plan_sources = (
            "me.",
            "wham_usage.",
            "codex_usage.",
            "id_token.",
            "access_token.",
        )
        if any(plan_source.startswith(prefix) for prefix in trusted_plan_sources):
            current_sub = _normalize_subscription_input(account.subscription_type)
            detected_sub = _plan_to_subscription_type(overview.get("plan_type"))
            # 避免把本地已确认的付费订阅（plus/team）被远端偶发 free/basic 覆盖降级。
            if detected_sub and current_sub != detected_sub:
                account.subscription_type = detected_sub
                account.subscription_at = utcnow_naive() if detected_sub else None
                updated = True
            elif not detected_sub and current_sub in PAID_SUBSCRIPTION_TYPES:
                logger.info(
                    "总览订阅同步跳过降级: email=%s current=%s detected=%s source=%s",
                    account.email,
                    current_sub,
                    detected_sub or "free/basic",
                    plan_source,
                )

        merged_extra = dict(extra_data)
        merged_extra[OVERVIEW_EXTRA_DATA_KEY] = overview
        account.extra_data = merged_extra
        updated = True
        return overview, updated
    except Exception as exc:
        logger.warning(f"刷新账号[{account.email}]总览失败: {exc}")
        if cached:
            stale_cached = dict(cached)
            stale_cached["stale"] = True
            stale_cached["error"] = str(exc)
            return stale_cached, updated
        return _fallback_overview(account, error_message=str(exc), stale=True), updated


# ============== API Endpoints ==============

@router.post("", response_model=AccountResponse)
async def create_manual_account(request: ManualAccountCreateRequest):
    """
    手动新增账号（邮箱 + 密码）。
    """
    email = (request.email or "").strip().lower()
    password = (request.password or "").strip()
    email_service = (request.email_service or "manual").strip() or "manual"
    status = request.status or AccountStatus.ACTIVE.value
    source = (request.source or "manual").strip() or "manual"
    subscription_type = _normalize_subscription_input(request.subscription_type)

    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="邮箱格式不正确")
    if not password:
        raise HTTPException(status_code=400, detail="密码不能为空")
    if status not in [e.value for e in AccountStatus]:
        raise HTTPException(status_code=400, detail="无效的状态值")

    with get_db() as db:
        exists = crud.get_account_by_email(db, email)
        if exists:
            raise HTTPException(status_code=409, detail="该邮箱账号已存在")

        try:
            account = crud.create_account(
                db,
                email=email,
                password=password,
                email_service=email_service,
                status=status,
                source=source,
                client_id=request.client_id,
                account_id=request.account_id,
                workspace_id=request.workspace_id,
                access_token=request.access_token,
                refresh_token=request.refresh_token,
                id_token=request.id_token,
                session_token=request.session_token,
                cookies=request.cookies,
                proxy_used=request.proxy_used,
                extra_data=request.metadata or {},
            )
            if subscription_type:
                account.subscription_type = subscription_type
                account.subscription_at = utcnow_naive()
                db.commit()
                db.refresh(account)
        except Exception as exc:
            logger.error(f"手动创建账号失败: {exc}")
            raise HTTPException(status_code=500, detail="创建账号失败")

        return account_to_response(account)


@router.post("/import")
async def import_accounts(request: ImportAccountsRequest):
    """
    一键导入账号（账号总览卡片使用）。
    支持按账号详情字段导入；可选覆盖同邮箱已有账号。
    """
    items = request.accounts or []
    if not items:
        raise HTTPException(status_code=400, detail="导入数据为空")

    max_import = 1000
    if len(items) > max_import:
        raise HTTPException(status_code=400, detail=f"单次最多导入 {max_import} 条")

    result = {
        "success": True,
        "total": len(items),
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "failed": 0,
        "errors": [],
    }

    def _safe_text(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text if text else None

    with get_db() as db:
        for index, raw_item in enumerate(items, start=1):
            if not isinstance(raw_item, dict):
                result["failed"] += 1
                result["errors"].append(
                    {"index": index, "email": "-", "error": "导入项必须是 JSON 对象"}
                )
                continue

            try:
                item = AccountImportItem.model_validate(raw_item)
            except Exception as exc:
                result["failed"] += 1
                result["errors"].append(
                    {"index": index, "email": str(raw_item.get("email") or "-"), "error": f"字段格式错误: {exc}"}
                )
                continue

            token_bundle = item.tokens if isinstance(item.tokens, dict) else {}
            access_token = _pick_first_text(item.access_token, token_bundle.get("access_token"), token_bundle.get("accessToken"))
            refresh_token = _pick_first_text(item.refresh_token, token_bundle.get("refresh_token"), token_bundle.get("refreshToken"))
            id_token = _pick_first_text(item.id_token, token_bundle.get("id_token"), token_bundle.get("idToken"))
            session_token = _pick_first_text(
                item.session_token,
                token_bundle.get("session_token"),
                token_bundle.get("sessionToken"),
            )
            client_id = _pick_first_text(item.client_id, token_bundle.get("client_id"), token_bundle.get("clientId"))

            access_claims = _decode_jwt_payload_unverified(access_token)
            id_claims = _decode_jwt_payload_unverified(id_token)

            auth_claims = {}
            for claims in (access_claims, id_claims):
                auth_obj = _get_nested(claims, ["https://api.openai.com/auth"])
                if isinstance(auth_obj, dict):
                    auth_claims = auth_obj
                    break

            account_id_value = _pick_first_text(
                item.account_id,
                raw_item.get("account_id"),
                auth_claims.get("chatgpt_account_id"),
            )
            workspace_id_value = _pick_first_text(
                item.workspace_id,
                raw_item.get("workspace_id"),
                account_id_value,
            )

            if not client_id:
                id_aud = id_claims.get("aud")
                id_aud_first = id_aud[0] if isinstance(id_aud, list) and id_aud else None
                client_id = _pick_first_text(
                    access_claims.get("client_id"),
                    id_aud_first,
                )

            email = str(item.email or "").strip().lower()
            if not email or "@" not in email:
                result["failed"] += 1
                result["errors"].append({"index": index, "email": email or "-", "error": "邮箱格式不正确"})
                continue

            status = str(item.status or AccountStatus.ACTIVE.value).strip().lower()
            if status not in [e.value for e in AccountStatus]:
                status = AccountStatus.ACTIVE.value

            email_service = str(item.email_service or "manual").strip() or "manual"
            source = str(item.source or "import").strip() or "import"
            subscription_type = (
                _normalize_subscription_input(item.subscription_type)
                or _normalize_subscription_input(item.plan_type)
                or _normalize_subscription_input(_pick_first_text(
                    raw_item.get("plan_type"),
                    auth_claims.get("chatgpt_plan_type"),
                ))
            )
            metadata = dict(item.metadata) if isinstance(item.metadata, dict) else {}
            for extra_key in (
                "id",
                "auth_mode",
                "user_id",
                "organization_id",
                "account_name",
                "account_structure",
                "quota",
                "tags",
                "created_at",
                "last_used",
                "usage_updated_at",
                "plan_type",
            ):
                value = raw_item.get(extra_key)
                if value is not None:
                    metadata[extra_key] = value
            if isinstance(token_bundle, dict) and token_bundle:
                metadata["tokens_shape"] = list(token_bundle.keys())

            exists = crud.get_account_by_email(db, email)
            if exists and not request.overwrite:
                result["skipped"] += 1
                continue

            try:
                if exists and request.overwrite:
                    update_payload = {
                        "password": _safe_text(item.password),
                        "email_service": email_service,
                        "status": status,
                        "client_id": _safe_text(client_id),
                        "account_id": _safe_text(account_id_value),
                        "workspace_id": _safe_text(workspace_id_value),
                        "access_token": _safe_text(access_token),
                        "refresh_token": _safe_text(refresh_token),
                        "id_token": _safe_text(id_token),
                        "session_token": _safe_text(session_token),
                        "cookies": item.cookies if item.cookies is not None else None,
                        "proxy_used": _safe_text(item.proxy_used),
                        "source": source,
                        "extra_data": metadata,
                        "last_refresh": utcnow_naive(),
                    }
                    clean_update_payload = {k: v for k, v in update_payload.items() if v is not None}
                    account = crud.update_account(db, exists.id, **clean_update_payload)
                    if account is None:
                        raise RuntimeError("更新账号失败")
                    account.subscription_type = subscription_type
                    account.subscription_at = utcnow_naive() if subscription_type else None
                    db.commit()
                    result["updated"] += 1
                    continue

                account = crud.create_account(
                    db,
                    email=email,
                    password=_safe_text(item.password),
                    client_id=_safe_text(client_id),
                    session_token=_safe_text(session_token),
                    email_service=email_service,
                    account_id=_safe_text(account_id_value),
                    workspace_id=_safe_text(workspace_id_value),
                    access_token=_safe_text(access_token),
                    refresh_token=_safe_text(refresh_token),
                    id_token=_safe_text(id_token),
                    cookies=item.cookies,
                    proxy_used=_safe_text(item.proxy_used),
                    extra_data=metadata,
                    status=status,
                    source=source,
                )
                if subscription_type:
                    account.subscription_type = subscription_type
                    account.subscription_at = utcnow_naive()
                    db.commit()
                result["created"] += 1
            except Exception as exc:
                result["failed"] += 1
                result["errors"].append({"index": index, "email": email, "error": str(exc)})

    return result


@router.get("", response_model=AccountListResponse)
async def list_accounts(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    status: Optional[str] = Query(None, description="状态筛选"),
    email_service: Optional[str] = Query(None, description="邮箱服务筛选"),
    search: Optional[str] = Query(None, description="搜索关键词"),
):
    """
    获取账号列表

    支持分页、状态筛选、邮箱服务筛选和搜索
    """
    with get_db() as db:
        # 构建查询
        query = db.query(Account)

        # 状态筛选
        if status:
            query = _apply_status_filter(query, status)

        # 邮箱服务筛选
        if email_service:
            query = query.filter(Account.email_service == email_service)

        # 搜索
        if search:
            search_pattern = f"%{search}%"
            query = query.filter(
                (Account.email.ilike(search_pattern)) |
                (Account.account_id.ilike(search_pattern))
            )

        # 统计总数
        total = query.count()

        # 分页
        offset = (page - 1) * page_size
        accounts = query.order_by(Account.created_at.desc()).offset(offset).limit(page_size).all()

        return AccountListResponse(
            total=total,
            accounts=[account_to_response(acc) for acc in accounts]
        )


@router.get("/overview/cards")
async def list_accounts_overview_cards(
    refresh: bool = Query(False, description="是否强制刷新远端配额"),
    search: Optional[str] = Query(None, description="按邮箱搜索"),
    status: Optional[str] = Query(None, description="状态筛选"),
    email_service: Optional[str] = Query(None, description="邮箱服务筛选"),
    proxy: Optional[str] = Query(None, description="可选代理地址"),
):
    """
    账号总览卡片数据。
    """
    with get_db() as db:
        query = db.query(Account).filter(
            func.lower(Account.subscription_type).in_(PAID_SUBSCRIPTION_TYPES)
        )
        if search:
            pattern = f"%{search}%"
            query = query.filter((Account.email.ilike(pattern)) | (Account.account_id.ilike(pattern)))
        if status:
            query = _apply_status_filter(query, status)
        if email_service:
            query = query.filter(Account.email_service == email_service)

        accounts = [
            account
            for account in query.order_by(Account.created_at.desc()).all()
            if not _is_overview_card_removed(account)
        ]
        current_account_id = _get_current_account_id(db)
        global_proxy = _get_proxy(proxy)
        # 卡片列表接口默认“缓存优先”，避免首次进入或新增卡片后触发全量远端请求造成页面卡死。
        # 需要强制刷新时统一走 /overview/refresh。
        allow_network = False
        if refresh:
            logger.info("overview/cards 接口忽略 refresh 参数，改由 /overview/refresh 执行远端刷新")

        rows = []
        db_updated = False

        for account in accounts:
            account_proxy = (account.proxy_used or "").strip() or global_proxy
            overview, updated = _get_account_overview_data(
                db,
                account,
                force_refresh=refresh,
                proxy=account_proxy,
                allow_network=allow_network,
            )
            db_updated = db_updated or updated

            overview_plan_raw = overview.get("plan_type")
            db_plan_raw = account.subscription_type
            has_db_subscription = bool(str(db_plan_raw or "").strip())
            # 与账号管理保持一致：卡片套餐优先使用 DB 的 subscription_type。
            effective_plan_raw = db_plan_raw if has_db_subscription else overview_plan_raw
            effective_plan_source = (
                "db.subscription_type"
                if has_db_subscription
                else (overview.get("plan_source") or "default")
            )
            if not _is_paid_subscription(effective_plan_raw):
                # Codex 账号管理仅允许 plus/team 账号进入。
                continue

            rows.append(
                {
                    "id": account.id,
                    "email": account.email,
                    "status": account.status,
                    "email_service": account.email_service,
                    "created_at": account.created_at.isoformat() if account.created_at else None,
                    "last_refresh": account.last_refresh.isoformat() if account.last_refresh else None,
                    "current": account.id == current_account_id,
                    "has_access_token": bool(account.access_token),
                    "plan_type": _normalize_plan_type(effective_plan_raw),
                    "plan_source": effective_plan_source,
                    "has_plus_or_team": _plan_to_subscription_type(effective_plan_raw) is not None,
                    "hourly_quota": overview.get("hourly_quota") or _build_unknown_quota(),
                    "weekly_quota": overview.get("weekly_quota") or _build_unknown_quota(),
                    "code_review_quota": overview.get("code_review_quota") or _build_unknown_quota(),
                    "overview_fetched_at": overview.get("fetched_at"),
                    "overview_stale": bool(overview.get("stale")),
                    "overview_error": overview.get("error"),
                }
            )

        if db_updated:
            db.commit()

        return {
            "total": len(rows),
            "current_account_id": current_account_id,
            "cache_ttl_seconds": OVERVIEW_CACHE_TTL_SECONDS,
            "network_mode": "refresh" if allow_network else "cache_only",
            "proxy": global_proxy or None,
            "accounts": rows,
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
        }


@router.get("/overview/cards/addable")
async def list_accounts_overview_addable(
    search: Optional[str] = Query(None, description="按邮箱搜索"),
    status: Optional[str] = Query(None, description="状态筛选"),
    email_service: Optional[str] = Query(None, description="邮箱服务筛选"),
):
    """读取已从卡片删除的账号，用于“添加账号”里重新添加。"""
    with get_db() as db:
        query = db.query(Account)
        if search:
            pattern = f"%{search}%"
            query = query.filter((Account.email.ilike(pattern)) | (Account.account_id.ilike(pattern)))
        if status:
            query = _apply_status_filter(query, status)
        if email_service:
            query = query.filter(Account.email_service == email_service)

        accounts = query.order_by(Account.created_at.desc()).all()
        rows = []
        for account in accounts:
            if not _is_overview_card_removed(account):
                continue
            if not _is_paid_subscription(account.subscription_type):
                continue
            rows.append(
                {
                    "id": account.id,
                    "email": account.email,
                    "status": account.status,
                    "email_service": account.email_service,
                    "subscription_type": account.subscription_type or "free",
                    "has_access_token": bool(account.access_token),
                    "created_at": account.created_at.isoformat() if account.created_at else None,
                }
            )

        return {
            "total": len(rows),
            "accounts": rows,
        }


@router.get("/overview/cards/selectable")
async def list_accounts_overview_selectable(
    search: Optional[str] = Query(None, description="按邮箱搜索"),
    status: Optional[str] = Query(None, description="状态筛选"),
    email_service: Optional[str] = Query(None, description="邮箱服务筛选"),
):
    """读取账号管理中的可选账号，用于账号总览添加/重新添加。"""
    with get_db() as db:
        query = db.query(Account)
        if search:
            pattern = f"%{search}%"
            query = query.filter((Account.email.ilike(pattern)) | (Account.account_id.ilike(pattern)))
        if status:
            query = _apply_status_filter(query, status)
        if email_service:
            query = query.filter(Account.email_service == email_service)

        accounts = query.order_by(Account.created_at.desc()).all()
        rows = []
        for account in accounts:
            # 仅返回当前未在卡片中的账号（即已从卡片移除）
            if not _is_overview_card_removed(account):
                continue
            if not _is_paid_subscription(account.subscription_type):
                continue
            rows.append(
                {
                    "id": account.id,
                    "email": account.email,
                    "password": account.password or "",
                    "status": account.status,
                    "email_service": account.email_service,
                    "subscription_type": account.subscription_type or "free",
                    "client_id": account.client_id or "",
                    "account_id": account.account_id or "",
                    "workspace_id": account.workspace_id or "",
                    "has_access_token": bool(account.access_token),
                    "created_at": account.created_at.isoformat() if account.created_at else None,
                }
            )

        return {
            "total": len(rows),
            "accounts": rows,
        }


@router.post("/overview/cards/remove")
async def remove_accounts_overview_cards(request: OverviewCardDeleteRequest):
    """从账号总览卡片移除（软删除，不影响账号管理列表）。"""
    with get_db() as db:
        ids = resolve_account_ids(
            db,
            request.ids,
            request.select_all,
            request.status_filter,
            request.email_service_filter,
            request.search_filter,
        )
        removed_count = 0
        missing_ids = []
        for account_id in ids:
            account = crud.get_account_by_id(db, account_id)
            if not account:
                missing_ids.append(account_id)
                continue
            if not _is_overview_card_removed(account):
                removed_count += 1
            _set_overview_card_removed(account, True)

        db.commit()
        return {
            "success": True,
            "removed_count": removed_count,
            "total": len(ids),
            "missing_ids": missing_ids,
        }


@router.post("/overview/cards/{account_id}/restore")
async def restore_accounts_overview_card(account_id: int):
    """恢复单个已删除的总览卡片。"""
    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")
        if not _is_paid_subscription(account.subscription_type):
            raise HTTPException(status_code=400, detail="仅 plus/team 账号可进入 Codex 账号管理")

        _set_overview_card_removed(account, False)
        db.commit()
        return {"success": True, "id": account.id, "email": account.email}


@router.post("/overview/cards/{account_id}/attach")
async def attach_accounts_overview_card(account_id: int):
    """从账号管理选择账号附加到总览卡片（已存在时保持幂等）。"""
    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")
        if not _is_paid_subscription(account.subscription_type):
            raise HTTPException(status_code=400, detail="仅 plus/team 账号可进入 Codex 账号管理")

        was_removed = _is_overview_card_removed(account)
        _set_overview_card_removed(account, False)
        db.commit()
        return {
            "success": True,
            "id": account.id,
            "email": account.email,
            "already_in_cards": not was_removed,
        }


@router.post("/overview/refresh")
async def refresh_accounts_overview(request: OverviewRefreshRequest):
    """
    批量刷新账号总览数据。
    """
    proxy = _get_proxy(request.proxy)
    result = {"success_count": 0, "failed_count": 0, "details": []}

    with get_db() as db:
        ids = resolve_account_ids(
            db,
            request.ids,
            request.select_all,
            request.status_filter,
            request.email_service_filter,
            request.search_filter,
        )
        if not ids:
            # 默认仅刷新“卡片里可见的付费账号”，避免无关账号导致全量阻塞。
            candidates = db.query(Account).filter(
                func.lower(Account.subscription_type).in_(PAID_SUBSCRIPTION_TYPES)
            ).order_by(Account.created_at.desc()).all()
            ids = [acc.id for acc in candidates if not _is_overview_card_removed(acc)]

        logger.info(
            "账号总览刷新开始: target_count=%s force=%s select_all=%s proxy=%s",
            len(ids),
            bool(request.force),
            bool(request.select_all),
            proxy or "-",
        )

        for account_id in ids:
            account = crud.get_account_by_id(db, account_id)
            if not account:
                result["failed_count"] += 1
                result["details"].append({"id": account_id, "success": False, "error": "账号不存在"})
                logger.warning("账号总览刷新失败: account_id=%s error=账号不存在", account_id)
                continue
            if (not _is_paid_subscription(account.subscription_type)) or _is_overview_card_removed(account):
                result["details"].append(
                    {
                        "id": account.id,
                        "email": account.email,
                        "success": False,
                        "error": "账号不在 Codex 卡片范围内，已跳过",
                    }
                )
                continue

            account_proxy = (account.proxy_used or "").strip() or proxy
            overview, updated = _get_account_overview_data(
                db,
                account,
                force_refresh=request.force,
                proxy=account_proxy,
                allow_network=True,
            )
            if updated:
                db.commit()

            if overview.get("hourly_quota", {}).get("status") == "unknown" and overview.get("weekly_quota", {}).get("status") == "unknown":
                result["failed_count"] += 1
                result["details"].append(
                    {
                        "id": account.id,
                        "email": account.email,
                        "success": False,
                        "error": overview.get("error") or "未获取到配额数据",
                    }
                )
                logger.warning(
                    "账号总览刷新失败: account_id=%s email=%s error=%s",
                    account.id,
                    account.email,
                    overview.get("error") or "未获取到配额数据",
                )
            else:
                result["success_count"] += 1
                result["details"].append(
                    {
                        "id": account.id,
                        "email": account.email,
                        "success": True,
                        "plan_type": overview.get("plan_type"),
                    }
                )
                logger.info(
                    "账号总览刷新成功: account_id=%s email=%s plan=%s hourly=%s weekly=%s code_review=%s hourly_source=%s weekly_source=%s",
                    account.id,
                    account.email,
                    overview.get("plan_type") or "-",
                    overview.get("hourly_quota", {}).get("percentage"),
                    overview.get("weekly_quota", {}).get("percentage"),
                    overview.get("code_review_quota", {}).get("percentage"),
                    overview.get("hourly_quota", {}).get("source"),
                    overview.get("weekly_quota", {}).get("source"),
                )

        logger.info(
            "账号总览刷新完成: success=%s failed=%s",
            result["success_count"],
            result["failed_count"],
        )

    return result


@router.get("/current")
async def get_current_account():
    """获取当前已切换的账号"""
    with get_db() as db:
        current_id = _get_current_account_id(db)
        if not current_id:
            return {"current_account_id": None, "account": None}
        account = crud.get_account_by_id(db, current_id)
        if not account:
            return {"current_account_id": None, "account": None}
        return {
            "current_account_id": account.id,
            "account": {
                "id": account.id,
                "email": account.email,
                "status": account.status,
                "email_service": account.email_service,
                "plan_type": _normalize_plan_type(account.subscription_type),
            },
        }


@router.post("/{account_id}/switch")
async def switch_current_account(account_id: int):
    """
    一键切换当前账号。
    """
    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")

        _set_current_account_id(db, account_id)
        snapshot_path = _write_current_account_snapshot(account)

        return {
            "success": True,
            "current_account_id": account_id,
            "email": account.email,
            "snapshot_file": snapshot_path,
        }


@router.get("/{account_id}", response_model=AccountResponse)
async def get_account(account_id: int):
    """获取单个账号详情"""
    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")
        return account_to_response(account)


@router.get("/{account_id}/tokens")
async def get_account_tokens(account_id: int):
    """获取账号的 Token 信息"""
    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")

        resolved_session_token = _resolve_account_session_token(account)
        session_source = "db" if str(account.session_token or "").strip() else ("cookies" if resolved_session_token else "none")

        # 若 DB 为空但 cookies 可解析到 session_token，自动回写，避免后续重复解析。
        if resolved_session_token and not str(account.session_token or "").strip():
            account.session_token = resolved_session_token
            account.last_refresh = utcnow_naive()
            db.commit()
            db.refresh(account)

        return {
            "id": account.id,
            "email": account.email,
            "access_token": account.access_token,
            "refresh_token": account.refresh_token,
            "id_token": account.id_token,
            "session_token": resolved_session_token,
            "session_token_source": session_source,
            "device_id": _resolve_account_device_id(account),
            "has_tokens": bool(account.access_token and account.refresh_token),
        }


@router.patch("/{account_id}", response_model=AccountResponse)
async def update_account(account_id: int, request: AccountUpdateRequest):
    """更新账号状态"""
    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")

        update_data = {}
        if request.status:
            if request.status not in [e.value for e in AccountStatus]:
                raise HTTPException(status_code=400, detail="无效的状态值")
            update_data["status"] = request.status

        if request.metadata:
            current_metadata = account.metadata or {}
            current_metadata.update(request.metadata)
            update_data["metadata"] = current_metadata

        if request.cookies is not None:
            # 留空则清空，非空则更新
            update_data["cookies"] = request.cookies or None

        if request.session_token is not None:
            # 留空则清空，非空则更新
            update_data["session_token"] = request.session_token or None
            update_data["last_refresh"] = utcnow_naive()

        account = crud.update_account(db, account_id, **update_data)
        return account_to_response(account)


@router.get("/{account_id}/cookies")
async def get_account_cookies(account_id: int):
    """获取账号的 cookie 字符串（仅供支付使用）"""
    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")
        return {"account_id": account_id, "cookies": account.cookies or ""}


@router.delete("/{account_id}")
async def delete_account(account_id: int):
    """删除单个账号"""
    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")

        crud.delete_account(db, account_id)
        return {"success": True, "message": f"账号 {account.email} 已删除"}


@router.post("/batch-delete")
async def batch_delete_accounts(request: BatchDeleteRequest):
    """批量删除账号"""
    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )
        deleted_count = 0
        errors = []

        for account_id in ids:
            try:
                account = crud.get_account_by_id(db, account_id)
                if account:
                    crud.delete_account(db, account_id)
                    deleted_count += 1
            except Exception as e:
                errors.append(f"ID {account_id}: {str(e)}")

        return {
            "success": True,
            "deleted_count": deleted_count,
            "errors": errors if errors else None
        }


@router.post("/batch-update")
async def batch_update_accounts(request: BatchUpdateRequest):
    """批量更新账号状态"""
    if request.status not in [e.value for e in AccountStatus]:
        raise HTTPException(status_code=400, detail="无效的状态值")

    with get_db() as db:
        updated_count = 0
        errors = []

        for account_id in request.ids:
            try:
                account = crud.get_account_by_id(db, account_id)
                if account:
                    crud.update_account(db, account_id, status=request.status)
                    updated_count += 1
            except Exception as e:
                errors.append(f"ID {account_id}: {str(e)}")

        return {
            "success": True,
            "updated_count": updated_count,
            "errors": errors if errors else None
        }


class BatchExportRequest(BaseModel):
    """批量导出请求"""
    ids: List[int] = []
    select_all: bool = False
    status_filter: Optional[str] = None
    email_service_filter: Optional[str] = None
    search_filter: Optional[str] = None


@router.post("/export/json")
async def export_accounts_json(request: BatchExportRequest):
    """导出账号为 JSON 格式"""
    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )
        accounts = db.query(Account).filter(Account.id.in_(ids)).all()

        export_data = []
        for acc in accounts:
            export_data.append({
                "email": acc.email,
                "password": acc.password,
                "client_id": acc.client_id,
                "account_id": acc.account_id,
                "workspace_id": acc.workspace_id,
                "access_token": acc.access_token,
                "refresh_token": acc.refresh_token,
                "id_token": acc.id_token,
                "session_token": acc.session_token,
                "email_service": acc.email_service,
                "registered_at": acc.registered_at.isoformat() if acc.registered_at else None,
                "last_refresh": acc.last_refresh.isoformat() if acc.last_refresh else None,
                "expires_at": acc.expires_at.isoformat() if acc.expires_at else None,
                "status": acc.status,
            })

        # 生成文件名
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"accounts_{timestamp}.json"

        # 返回 JSON 响应
        content = json.dumps(export_data, ensure_ascii=False, indent=2)

        return StreamingResponse(
            iter([content]),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )


@router.post("/export/csv")
async def export_accounts_csv(request: BatchExportRequest):
    """导出账号为 CSV 格式"""
    import csv
    import io

    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )
        accounts = db.query(Account).filter(Account.id.in_(ids)).all()

        # 创建 CSV 内容
        output = io.StringIO()
        writer = csv.writer(output)

        # 写入表头
        writer.writerow([
            "ID", "Email", "Password", "Client ID",
            "Account ID", "Workspace ID",
            "Access Token", "Refresh Token", "ID Token", "Session Token",
            "Email Service", "Status", "Registered At", "Last Refresh", "Expires At"
        ])

        # 写入数据
        for acc in accounts:
            writer.writerow([
                acc.id,
                acc.email,
                acc.password or "",
                acc.client_id or "",
                acc.account_id or "",
                acc.workspace_id or "",
                acc.access_token or "",
                acc.refresh_token or "",
                acc.id_token or "",
                acc.session_token or "",
                acc.email_service,
                acc.status,
                acc.registered_at.isoformat() if acc.registered_at else "",
                acc.last_refresh.isoformat() if acc.last_refresh else "",
                acc.expires_at.isoformat() if acc.expires_at else ""
            ])

        # 生成文件名
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"accounts_{timestamp}.csv"

        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )


@router.post("/export/sub2api")
async def export_accounts_sub2api(request: BatchExportRequest):
    """导出账号为 Sub2Api 格式（所有选中账号合并到一个 JSON 的 accounts 数组中）"""

    def make_account_entry(acc) -> dict:
        expires_at = int(acc.expires_at.timestamp()) if acc.expires_at else 0
        return {
            "name": acc.email,
            "platform": "openai",
            "type": "oauth",
            "credentials": {
                "access_token": acc.access_token or "",
                "chatgpt_account_id": acc.account_id or "",
                "chatgpt_user_id": "",
                "client_id": acc.client_id or "",
                "expires_at": expires_at,
                "expires_in": 863999,
                "model_mapping": {
                    "gpt-5.1": "gpt-5.1",
                    "gpt-5.1-codex": "gpt-5.1-codex",
                    "gpt-5.1-codex-max": "gpt-5.1-codex-max",
                    "gpt-5.1-codex-mini": "gpt-5.1-codex-mini",
                    "gpt-5.2": "gpt-5.2",
                    "gpt-5.2-codex": "gpt-5.2-codex",
                    "gpt-5.3": "gpt-5.3",
                    "gpt-5.3-codex": "gpt-5.3-codex",
                    "gpt-5.4": "gpt-5.4"
                },
                "organization_id": acc.workspace_id or "",
                "refresh_token": acc.refresh_token or ""
            },
            "extra": {},
            "concurrency": 10,
            "priority": 1,
            "rate_multiplier": 1,
            "auto_pause_on_expired": True
        }

    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )
        accounts = db.query(Account).filter(Account.id.in_(ids)).all()

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        payload = {
            "proxies": [],
            "accounts": [make_account_entry(acc) for acc in accounts]
        }
        content = json.dumps(payload, ensure_ascii=False, indent=2)

        if len(accounts) == 1:
            filename = f"{accounts[0].email}_sub2api.json"
        else:
            filename = f"sub2api_tokens_{timestamp}.json"

        return StreamingResponse(
            iter([content]),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )


@router.post("/export/codex")
async def export_accounts_codex(request: BatchExportRequest):
    """????? Codex ???????"""
    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )
        accounts = db.query(Account).filter(Account.id.in_(ids)).all()

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        lines = []
        for acc in accounts:
            lines.append(json.dumps({
                "email": acc.email,
                "password": acc.password or "",
                "client_id": acc.client_id or "",
                "access_token": acc.access_token or "",
                "refresh_token": acc.refresh_token or "",
                "session_token": acc.session_token or "",
                "account_id": acc.account_id or "",
                "workspace_id": acc.workspace_id or "",
                "cookies": acc.cookies or "",
                "type": "codex",
                "source": getattr(acc, "source", None) or "manual",
            }, ensure_ascii=False))

        content = "\n".join(lines)
        filename = f"codex_accounts_{timestamp}.jsonl"
        return StreamingResponse(
            iter([content]),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )


@router.post("/export/cpa")
async def export_accounts_cpa(request: BatchExportRequest):
    """导出账号为 CPA Token JSON 格式（每个账号单独一个 JSON 文件，打包为 ZIP）"""
    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )
        accounts = db.query(Account).filter(Account.id.in_(ids)).all()

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        if len(accounts) == 1:
            # 单个账号直接返回 JSON 文件
            acc = accounts[0]
            token_data = generate_token_json(acc)
            content = json.dumps(token_data, ensure_ascii=False, indent=2)
            filename = f"{acc.email}.json"
            return StreamingResponse(
                iter([content]),
                media_type="application/json",
                headers={"Content-Disposition": f"attachment; filename={filename}"}
            )

        # 多个账号打包为 ZIP
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for acc in accounts:
                token_data = generate_token_json(acc)
                content = json.dumps(token_data, ensure_ascii=False, indent=2)
                zf.writestr(f"{acc.email}.json", content)

        zip_buffer.seek(0)
        zip_filename = f"cpa_tokens_{timestamp}.zip"
        return StreamingResponse(
            zip_buffer,
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename={zip_filename}"}
        )


@router.get("/stats/summary")
async def get_accounts_stats():
    """获取账号统计信息"""
    with get_db() as db:
        from sqlalchemy import func

        # 总数
        total = db.query(func.count(Account.id)).scalar()

        # 按状态统计
        status_stats = db.query(
            Account.status,
            func.count(Account.id)
        ).group_by(Account.status).all()

        # 按邮箱服务统计
        service_stats = db.query(
            Account.email_service,
            func.count(Account.id)
        ).group_by(Account.email_service).all()

        return {
            "total": total,
            "by_status": {status: count for status, count in status_stats},
            "by_email_service": {service: count for service, count in service_stats}
        }


@router.get("/stats/overview")
async def get_accounts_overview():
    """获取账号总览统计信息（用于总览页面）"""
    with get_db() as db:
        total = db.query(func.count(Account.id)).scalar() or 0
        active_count = db.query(func.count(Account.id)).filter(
            Account.status == AccountStatus.ACTIVE.value
        ).scalar() or 0

        with_access_token = db.query(func.count(Account.id)).filter(
            Account.access_token.isnot(None),
            Account.access_token != "",
        ).scalar() or 0
        with_refresh_token = db.query(func.count(Account.id)).filter(
            Account.refresh_token.isnot(None),
            Account.refresh_token != "",
        ).scalar() or 0
        without_access_token = max(total - with_access_token, 0)

        cpa_uploaded_count = db.query(func.count(Account.id)).filter(
            Account.cpa_uploaded.is_(True)
        ).scalar() or 0

        status_stats = db.query(
            Account.status,
            func.count(Account.id),
        ).group_by(Account.status).all()

        service_stats = db.query(
            Account.email_service,
            func.count(Account.id),
        ).group_by(Account.email_service).all()

        source_stats = db.query(
            Account.source,
            func.count(Account.id),
        ).group_by(Account.source).all()

        subscription_stats = db.query(
            Account.subscription_type,
            func.count(Account.id),
        ).group_by(Account.subscription_type).all()

        recent_accounts = db.query(Account).order_by(Account.created_at.desc()).limit(10).all()

        return {
            "total": total,
            "active_count": active_count,
            "token_stats": {
                "with_access_token": with_access_token,
                "with_refresh_token": with_refresh_token,
                "without_access_token": without_access_token,
            },
            "cpa_uploaded_count": cpa_uploaded_count,
            "by_status": {status or "unknown": count for status, count in status_stats},
            "by_email_service": {service or "unknown": count for service, count in service_stats},
            "by_source": {source or "unknown": count for source, count in source_stats},
            "by_subscription": {
                (subscription or "free"): count for subscription, count in subscription_stats
            },
            "recent_accounts": [
                {
                    "id": acc.id,
                    "email": acc.email,
                    "status": acc.status,
                    "email_service": acc.email_service,
                    "source": acc.source,
                    "subscription_type": acc.subscription_type or "free",
                    "created_at": acc.created_at.isoformat() if acc.created_at else None,
                    "last_refresh": acc.last_refresh.isoformat() if acc.last_refresh else None,
                }
                for acc in recent_accounts
            ],
        }


# ============== Token 刷新相关 ==============

class TokenRefreshRequest(BaseModel):
    """Token 刷新请求"""
    proxy: Optional[str] = None


class BatchRefreshRequest(BaseModel):
    """批量刷新请求"""
    ids: List[int] = []
    proxy: Optional[str] = None
    select_all: bool = False
    status_filter: Optional[str] = None
    email_service_filter: Optional[str] = None
    search_filter: Optional[str] = None


class TokenValidateRequest(BaseModel):
    """Token 验证请求"""
    proxy: Optional[str] = None


class BatchValidateRequest(BaseModel):
    """批量验证请求"""
    ids: List[int] = []
    proxy: Optional[str] = None
    select_all: bool = False
    status_filter: Optional[str] = None
    email_service_filter: Optional[str] = None
    search_filter: Optional[str] = None


@router.post("/batch-refresh")
async def batch_refresh_tokens(request: BatchRefreshRequest, background_tasks: BackgroundTasks):
    """批量刷新账号 Token"""
    proxy = _get_proxy(request.proxy)

    results = {
        "success_count": 0,
        "failed_count": 0,
        "errors": []
    }

    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )

    for account_id in ids:
        try:
            result = do_refresh(account_id, proxy)
            if result.success:
                results["success_count"] += 1
            else:
                results["failed_count"] += 1
                results["errors"].append({"id": account_id, "error": result.error_message})
        except Exception as e:
            results["failed_count"] += 1
            results["errors"].append({"id": account_id, "error": str(e)})

    return results


@router.post("/{account_id}/refresh")
async def refresh_account_token(account_id: int, request: Optional[TokenRefreshRequest] = Body(default=None)):
    """刷新单个账号的 Token"""
    proxy = _get_proxy(request.proxy if request else None)
    result = do_refresh(account_id, proxy)

    if result.success:
        return {
            "success": True,
            "message": "Token 刷新成功",
            "expires_at": result.expires_at.isoformat() if result.expires_at else None
        }
    else:
        return {
            "success": False,
            "error": result.error_message
        }


@router.post("/batch-validate")
async def batch_validate_tokens(request: BatchValidateRequest):
    """批量验证账号 Token 有效性"""
    proxy = _get_proxy(request.proxy)

    results = {
        "valid_count": 0,
        "invalid_count": 0,
        "details": []
    }

    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )

    for account_id in ids:
        try:
            is_valid, error = do_validate(account_id, proxy)
            results["details"].append({
                "id": account_id,
                "valid": is_valid,
                "error": error
            })
            if is_valid:
                results["valid_count"] += 1
            else:
                results["invalid_count"] += 1
        except Exception as e:
            # 异常账号兜底打标 failed，保证前端“失败”筛选可见。
            try:
                with get_db() as db:
                    account = crud.get_account_by_id(db, account_id)
                    if account and account.status != AccountStatus.FAILED.value:
                        crud.update_account(db, account_id, status=AccountStatus.FAILED.value)
            except Exception:
                pass
            results["invalid_count"] += 1
            results["details"].append({
                "id": account_id,
                "valid": False,
                "error": str(e)
            })

    return results


@router.post("/{account_id}/validate")
async def validate_account_token(account_id: int, request: Optional[TokenValidateRequest] = Body(default=None)):
    """验证单个账号的 Token 有效性"""
    proxy = _get_proxy(request.proxy if request else None)
    is_valid, error = do_validate(account_id, proxy)

    return {
        "id": account_id,
        "valid": is_valid,
        "error": error
    }


# ============== CPA 上传相关 ==============

class CPAUploadRequest(BaseModel):
    """CPA 上传请求"""
    proxy: Optional[str] = None
    cpa_service_id: Optional[int] = None  # 指定 CPA 服务 ID，不传则使用全局配置


class BatchCPAUploadRequest(BaseModel):
    """批量 CPA 上传请求"""
    ids: List[int] = []
    proxy: Optional[str] = None
    select_all: bool = False
    status_filter: Optional[str] = None
    email_service_filter: Optional[str] = None
    search_filter: Optional[str] = None
    cpa_service_id: Optional[int] = None  # 指定 CPA 服务 ID，不传则使用全局配置


@router.post("/batch-upload-cpa")
async def batch_upload_accounts_to_cpa(request: BatchCPAUploadRequest):
    """批量上传账号到 CPA"""

    proxy = request.proxy if request.proxy else get_settings().proxy_url

    # 解析指定的 CPA 服务
    cpa_api_url = None
    cpa_api_token = None
    if request.cpa_service_id:
        with get_db() as db:
            svc = crud.get_cpa_service_by_id(db, request.cpa_service_id)
            if not svc:
                raise HTTPException(status_code=404, detail="指定的 CPA 服务不存在")
            cpa_api_url = svc.api_url
            cpa_api_token = svc.api_token

    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )

    results = batch_upload_to_cpa(ids, proxy, api_url=cpa_api_url, api_token=cpa_api_token)
    return results


@router.post("/{account_id}/upload-cpa")
async def upload_account_to_cpa(account_id: int, request: Optional[CPAUploadRequest] = Body(default=None)):
    """上传单个账号到 CPA"""

    proxy = request.proxy if request and request.proxy else get_settings().proxy_url
    cpa_service_id = request.cpa_service_id if request else None

    # 解析指定的 CPA 服务
    cpa_api_url = None
    cpa_api_token = None
    if cpa_service_id:
        with get_db() as db:
            svc = crud.get_cpa_service_by_id(db, cpa_service_id)
            if not svc:
                raise HTTPException(status_code=404, detail="指定的 CPA 服务不存在")
            cpa_api_url = svc.api_url
            cpa_api_token = svc.api_token

    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")

        if not account.access_token:
            return {
                "success": False,
                "error": "账号缺少 Token，无法上传"
            }

        # 生成 Token JSON
        token_data = generate_token_json(account)

        # 上传
        success, message = upload_to_cpa(token_data, proxy, api_url=cpa_api_url, api_token=cpa_api_token)

        if success:
            account.cpa_uploaded = True
            account.cpa_uploaded_at = utcnow_naive()
            db.commit()
            return {"success": True, "message": message}
        else:
            return {"success": False, "error": message}


class Sub2ApiUploadRequest(BaseModel):
    """单账号 Sub2API 上传请求"""
    service_id: Optional[int] = None
    concurrency: int = 3
    priority: int = 50


class BatchSub2ApiUploadRequest(BaseModel):
    """批量 Sub2API 上传请求"""
    ids: List[int] = []
    select_all: bool = False
    status_filter: Optional[str] = None
    email_service_filter: Optional[str] = None
    search_filter: Optional[str] = None
    service_id: Optional[int] = None  # 指定 Sub2API 服务 ID，不传则使用第一个启用的
    concurrency: int = 3
    priority: int = 50


@router.post("/batch-upload-sub2api")
async def batch_upload_accounts_to_sub2api(request: BatchSub2ApiUploadRequest):
    """批量上传账号到 Sub2API"""

    # 解析指定的 Sub2API 服务
    api_url = None
    api_key = None
    if request.service_id:
        with get_db() as db:
            svc = crud.get_sub2api_service_by_id(db, request.service_id)
            if not svc:
                raise HTTPException(status_code=404, detail="指定的 Sub2API 服务不存在")
            api_url = svc.api_url
            api_key = svc.api_key
    else:
        with get_db() as db:
            svcs = crud.get_sub2api_services(db, enabled=True)
            if svcs:
                api_url = svcs[0].api_url
                api_key = svcs[0].api_key

    if not api_url or not api_key:
        raise HTTPException(status_code=400, detail="未找到可用的 Sub2API 服务，请先在设置中配置")

    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )

    results = batch_upload_to_sub2api(
        ids, api_url, api_key,
        concurrency=request.concurrency,
        priority=request.priority,
        target_type=locals().get("target_type", "sub2api"),
    )
    return results


@router.post("/{account_id}/upload-sub2api")
async def upload_account_to_sub2api(account_id: int, request: Optional[Sub2ApiUploadRequest] = Body(default=None)):
    """上传单个账号到 Sub2API"""

    service_id = request.service_id if request else None
    concurrency = request.concurrency if request else 3
    priority = request.priority if request else 50

    api_url = None
    api_key = None
    if service_id:
        with get_db() as db:
            svc = crud.get_sub2api_service_by_id(db, service_id)
            if not svc:
                raise HTTPException(status_code=404, detail="指定的 Sub2API 服务不存在")
            api_url = svc.api_url
            api_key = svc.api_key
    else:
        with get_db() as db:
            svcs = crud.get_sub2api_services(db, enabled=True)
            if svcs:
                api_url = svcs[0].api_url
                api_key = svcs[0].api_key

    if not api_url or not api_key:
        raise HTTPException(status_code=400, detail="未找到可用的 Sub2API 服务，请先在设置中配置")

    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")
        if not account.access_token:
            return {"success": False, "error": "账号缺少 Token，无法上传"}

        success, message = upload_to_sub2api(
            [account], api_url, api_key,
            concurrency=concurrency, priority=priority,
            target_type=locals().get("target_type", "sub2api")
        )
        if success:
            return {"success": True, "message": message}
        else:
            return {"success": False, "error": message}


# ============== Team Manager 上传 ==============

class UploadTMRequest(BaseModel):
    service_id: Optional[int] = None


class BatchUploadTMRequest(BaseModel):
    ids: List[int] = []
    select_all: bool = False
    status_filter: Optional[str] = None
    email_service_filter: Optional[str] = None
    search_filter: Optional[str] = None
    service_id: Optional[int] = None


@router.post("/batch-upload-tm")
async def batch_upload_accounts_to_tm(request: BatchUploadTMRequest):
    """批量上传账号到 Team Manager"""

    with get_db() as db:
        if request.service_id:
            svc = crud.get_tm_service_by_id(db, request.service_id)
        else:
            svcs = crud.get_tm_services(db, enabled=True)
            svc = svcs[0] if svcs else None

        if not svc:
            raise HTTPException(status_code=400, detail="未找到可用的 Team Manager 服务，请先在设置中配置")

        api_url = svc.api_url
        api_key = svc.api_key
        target_type = getattr(svc, "target_type", "sub2api")

        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )

    results = batch_upload_to_team_manager(ids, api_url, api_key)
    return results


@router.post("/{account_id}/upload-tm")
async def upload_account_to_tm(account_id: int, request: Optional[UploadTMRequest] = Body(default=None)):
    """上传单账号到 Team Manager"""

    service_id = request.service_id if request else None

    with get_db() as db:
        if service_id:
            svc = crud.get_tm_service_by_id(db, service_id)
        else:
            svcs = crud.get_tm_services(db, enabled=True)
            svc = svcs[0] if svcs else None

        if not svc:
            raise HTTPException(status_code=400, detail="未找到可用的 Team Manager 服务，请先在设置中配置")

        api_url = svc.api_url
        api_key = svc.api_key
        target_type = getattr(svc, "target_type", "sub2api")

        account = crud.get_account_by_id(db, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")
        success, message = upload_to_team_manager(account, api_url, api_key)

    return {"success": success, "message": message}


# ============== Inbox Code ==============

def _build_inbox_config(db, service_type, email: str) -> dict:
    """根据账号邮箱服务类型从数据库构建服务配置（不传 proxy_url）"""
    from ...database.models import EmailService as EmailServiceModel
    from ...services import EmailServiceType as EST

    if service_type == EST.TEMPMAIL:
        settings = get_settings()
        return {
            "base_url": settings.tempmail_base_url,
            "timeout": settings.tempmail_timeout,
            "max_retries": settings.tempmail_max_retries,
        }

    if service_type == EST.YYDS_MAIL:
        settings = get_settings()
        return {
            "base_url": settings.yyds_mail_base_url,
            "api_key": settings.yyds_mail_api_key.get_secret_value() if settings.yyds_mail_api_key else "",
            "default_domain": settings.yyds_mail_default_domain,
            "timeout": settings.yyds_mail_timeout,
            "max_retries": settings.yyds_mail_max_retries,
        }

    if service_type == EST.MOE_MAIL:
        # 按域名后缀匹配，找不到则取 priority 最小的
        domain = email.split("@")[1] if "@" in email else ""
        services = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == "moe_mail",
            EmailServiceModel.enabled == True
        ).order_by(EmailServiceModel.priority.asc()).all()
        svc = None
        for s in services:
            cfg = s.config or {}
            if cfg.get("default_domain") == domain or cfg.get("domain") == domain:
                svc = s
                break
        if not svc and services:
            svc = services[0]
        if not svc:
            return None
        cfg = svc.config.copy()
        if "api_url" in cfg and "base_url" not in cfg:
            cfg["base_url"] = cfg.pop("api_url")
        return cfg

    # 其余服务类型：直接按 service_type 查数据库
    type_map = {
        EST.TEMP_MAIL: "temp_mail",
        EST.DUCK_MAIL: "duck_mail",
        EST.FREEMAIL: "freemail",
        EST.IMAP_MAIL: "imap_mail",
        EST.OUTLOOK: "outlook",
    }
    db_type = type_map.get(service_type)
    if not db_type:
        return None

    query = db.query(EmailServiceModel).filter(
        EmailServiceModel.service_type == db_type,
        EmailServiceModel.enabled == True
    )
    if service_type == EST.OUTLOOK:
        # 按 config.email 匹配账号 email
        services = query.all()
        svc = next((s for s in services if (s.config or {}).get("email") == email), None)
    else:
        svc = query.order_by(EmailServiceModel.priority.asc()).first()

    if not svc:
        return None
    cfg = svc.config.copy() if svc.config else {}
    if "api_url" in cfg and "base_url" not in cfg:
        cfg["base_url"] = cfg.pop("api_url")
    return cfg


@router.post("/{account_id}/inbox-code")
async def get_account_inbox_code(account_id: int):
    """查询账号邮箱收件箱最新验证码"""
    from ...services import EmailServiceFactory, EmailServiceType

    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")

        try:
            service_type = EmailServiceType(account.email_service)
        except ValueError:
            return {"success": False, "error": "不支持的邮箱服务类型"}

        config = _build_inbox_config(db, service_type, account.email)
        if config is None:
            return {"success": False, "error": "未找到可用的邮箱服务配置"}

        try:
            svc = EmailServiceFactory.create(service_type, config)
            code = svc.get_verification_code(
                account.email,
                email_id=account.email_service_id,
                timeout=12
            )
        except Exception as e:
            return {"success": False, "error": str(e)}

        if not code:
            return {"success": False, "error": "未收到验证码邮件"}

        return {"success": True, "code": code, "email": account.email}
