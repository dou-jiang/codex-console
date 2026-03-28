"""
new-api 账号上传功能
"""

import json
import logging
from datetime import datetime, timezone
from typing import List, Tuple

from curl_cffi import requests as cffi_requests

from ...database.models import Account
from ...database.session import get_db

logger = logging.getLogger(__name__)

CHANNEL_TYPE_CODEX = 57
CHANNEL_GROUP_DEFAULT = "default"
DEFAULT_CODEX_MODELS = ['gpt-5', 'gpt-5-codex', 'gpt-5-codex-mini', 'gpt-5.1', 'gpt-5.1-codex', 'gpt-5.1-codex-max', 'gpt-5.1-codex-mini', 'gpt-5.2', 'gpt-5.2-codex', 'gpt-5.3-codex', 'gpt-5.3-codex-spark', 'gpt-5.4', 'gpt-5.4-mini']


def normalize_new_api_url(api_url: str) -> str:
    """规范化 new-api 根地址。"""
    return (api_url or "").rstrip("/")


def resolve_new_api_account_type(account: Account) -> str:
    """解析 new-api 账号类型。"""
    subscription_type = (getattr(account, "subscription_type", None) or "").lower()
    if subscription_type == "team":
        return "team"

    extra_data = getattr(account, "extra_data", None) or {}
    if isinstance(extra_data, dict):
        overview = extra_data.get("codex_overview") or {}
        if isinstance(overview, dict):
            plan_type = str(overview.get("plan_type") or "").lower()
            if "codex" in plan_type:
                return "codex"
        for key in ("account_type", "type", "plan_type", "subscription_type"):
            value = str(extra_data.get(key) or "").lower()
            if "codex" in value:
                return "codex"
            if value in {"team", "plus", "pro", "oauth"}:
                return value

    if subscription_type in {"plus", "pro"}:
        return subscription_type
    return "oauth"


def build_new_api_channel_key(account: Account) -> str:
    """构建 new-api Codex 渠道 key。"""
    expired = account.expires_at.astimezone(timezone.utc).isoformat() if account.expires_at else ""
    payload = {
        "access_token": account.access_token or "",
        "refresh_token": account.refresh_token or "",
        "account_id": account.account_id or "",
        "email": account.email,
        "type": resolve_new_api_account_type(account),
        "expired": expired,
        "last_refresh": datetime.now(timezone.utc).isoformat(),
    }
    return json.dumps(payload, ensure_ascii=False)


def build_new_api_channel_payload(account: Account) -> dict:
    """构建 new-api 渠道创建载荷。"""
    return {
        "name": account.email,
        "type": CHANNEL_TYPE_CODEX,
        "key": build_new_api_channel_key(account),
        "group": CHANNEL_GROUP_DEFAULT,
        "models": ",".join(DEFAULT_CODEX_MODELS),
        "status": 1,
    }


def create_new_api_session(api_url: str, username: str, password: str):
    """创建已登录的 new-api 会话。"""
    session = cffi_requests.Session(impersonate="chrome110")
    url = normalize_new_api_url(api_url) + "/api/user/login"
    response = session.post(
        url,
        json={"username": username, "password": password},
        headers={"Content-Type": "application/json"},
        proxies=None,
        timeout=15,
    )
    return session, response


def ensure_new_api_login(api_url: str, username: str, password: str):
    """校验 new-api 管理员登录状态。"""
    if not api_url:
        return False, "new-api URL 未配置", None, None
    if not username:
        return False, "new-api 用户名未配置", None, None
    if not password:
        return False, "new-api 密码未配置", None, None

    session, response = create_new_api_session(api_url, username, password)
    if response.status_code != 200:
        return False, f"登录失败: HTTP {response.status_code}", None, None

    try:
        data = response.json()
    except Exception:
        return False, f"登录失败: {response.text[:200]}", None, None

    if not isinstance(data, dict) or not data.get("success"):
        return False, data.get("message", "登录失败") if isinstance(data, dict) else "登录失败", None, None

    user_data = data.get("data") or {}
    user_id = user_data.get("id") if isinstance(user_data, dict) else None
    if user_id is None:
        return False, "登录成功，但未返回用户 ID", None, None

    session.headers.update({"New-Api-User": str(user_id)})
    return True, "登录成功", session, user_id


def create_new_api_channel(api_url: str, session, account: Account) -> Tuple[bool, str]:
    """在 new-api 中创建 Codex 渠道。"""
    url = normalize_new_api_url(api_url) + "/api/channel/"
    payload = {
        "mode": "single",
        "channel": build_new_api_channel_payload(account),
    }
    response = session.post(
        url,
        json=payload,
        headers={"Content-Type": "application/json"},
        proxies=None,
        timeout=20,
    )
    if response.status_code != 200:
        return False, f"创建渠道失败: HTTP {response.status_code}"

    try:
        data = response.json()
    except Exception:
        return False, f"创建渠道失败: {response.text[:200]}"

    if isinstance(data, dict) and data.get("success"):
        return True, "渠道创建成功"
    if isinstance(data, dict):
        return False, data.get("message", "创建渠道失败")
    return False, "创建渠道失败"


def upload_to_new_api(accounts: List[Account], api_url: str, username: str, password: str) -> Tuple[bool, str]:
    """上传账号列表到 new-api 平台。"""
    if not accounts:
        return False, "无可上传的账号"

    valid_accounts = [account for account in accounts if account.access_token]
    if not valid_accounts:
        return False, "所有账号均缺少 access_token，无法上传"

    ok, message, session, _user_id = ensure_new_api_login(api_url, username, password)
    if not ok:
        return False, message

    success_count = 0
    errors = []
    for account in valid_accounts:
        created, created_message = create_new_api_channel(api_url, session, account)
        if created:
            success_count += 1
        else:
            errors.append(f"{account.email}: {created_message}")

    if success_count == len(valid_accounts):
        return True, f"成功上传 {success_count} 个账号"
    if success_count == 0:
        return False, "; ".join(errors[:3]) if errors else "上传失败"
    return False, f"部分上传成功({success_count}/{len(valid_accounts)}): {'; '.join(errors[:3])}"


def batch_upload_to_new_api(account_ids: List[int], api_url: str, username: str, password: str) -> dict:
    """批量上传指定 ID 的账号到 new-api 平台。"""
    results = {
        "success_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "details": [],
    }

    with get_db() as db:
        accounts = []
        for account_id in account_ids:
            account = db.query(Account).filter(Account.id == account_id).first()
            if not account:
                results["failed_count"] += 1
                results["details"].append({"id": account_id, "email": None, "success": False, "error": "账号不存在"})
                continue
            if not account.access_token:
                results["skipped_count"] += 1
                results["details"].append({"id": account_id, "email": account.email, "success": False, "error": "缺少 access_token"})
                continue
            accounts.append(account)

        if not accounts:
            return results

        success, message = upload_to_new_api(accounts, api_url, username, password)
        if success:
            for account in accounts:
                results["success_count"] += 1
                results["details"].append({"id": account.id, "email": account.email, "success": True, "message": message})
        else:
            for account in accounts:
                results["failed_count"] += 1
                results["details"].append({"id": account.id, "email": account.email, "success": False, "error": message})

    return results


def test_new_api_connection(api_url: str, username: str, password: str) -> Tuple[bool, str]:
    """测试 new-api 连接。"""
    ok, message, _session, _user_id = ensure_new_api_login(api_url, username, password)
    if not ok:
        return False, message
    return True, "new-api 连接测试成功"
