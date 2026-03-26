"""
CPA (Codex Protocol API) 上传功能
"""

import json
import logging
from typing import List, Dict, Any, Tuple, Optional
from datetime import datetime
from urllib.parse import quote

from curl_cffi import requests as cffi_requests
from curl_cffi import CurlMime

from ...database.session import get_db
from ...database.models import Account
from ...config.settings import get_settings

logger = logging.getLogger(__name__)


def _normalize_cpa_auth_files_url(api_url: str) -> str:
    """将用户填写的 CPA 地址规范化为 auth-files 接口地址。"""
    normalized = (api_url or "").strip().rstrip("/")
    lower_url = normalized.lower()

    if not normalized:
        return ""

    if lower_url.endswith("/auth-files"):
        return normalized

    if lower_url.endswith("/v0/management") or lower_url.endswith("/management"):
        return f"{normalized}/auth-files"

    if lower_url.endswith("/v0"):
        return f"{normalized}/management/auth-files"

    return f"{normalized}/v0/management/auth-files"


def _build_cpa_headers(api_token: str, content_type: Optional[str] = None) -> dict:
    headers = {
        "Authorization": f"Bearer {api_token}",
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def _extract_cpa_error(response) -> str:
    error_msg = f"上传失败: HTTP {response.status_code}"
    try:
        error_detail = response.json()
        if isinstance(error_detail, dict):
            error_msg = error_detail.get("message", error_msg)
    except Exception:
        error_msg = f"{error_msg} - {response.text[:200]}"
    return error_msg


def _post_cpa_auth_file_multipart(upload_url: str, filename: str, file_content: bytes, api_token: str):
    mime = CurlMime()
    mime.addpart(
        name="file",
        data=file_content,
        filename=filename,
        content_type="application/json",
    )

    return cffi_requests.post(
        upload_url,
        multipart=mime,
        headers=_build_cpa_headers(api_token),
        proxies=None,
        timeout=30,
        impersonate="chrome110",
    )


def _post_cpa_auth_file_raw_json(upload_url: str, filename: str, file_content: bytes, api_token: str):
    raw_upload_url = f"{upload_url}?name={quote(filename)}"
    return cffi_requests.post(
        raw_upload_url,
        data=file_content,
        headers=_build_cpa_headers(api_token, content_type="application/json"),
        proxies=None,
        timeout=30,
        impersonate="chrome110",
    )


def generate_token_json(account: Account) -> dict:
    """
    生成 CPA 格式的 Token JSON

    Args:
        account: 账号模型实例

    Returns:
        CPA 格式的 Token 字典
    """
    return {
        "type": "codex",
        "email": account.email,
        "expired": account.expires_at.strftime("%Y-%m-%dT%H:%M:%S+08:00") if account.expires_at else "",
        "id_token": account.id_token or "",
        "account_id": account.account_id or "",
        "access_token": account.access_token or "",
        "last_refresh": account.last_refresh.strftime("%Y-%m-%dT%H:%M:%S+08:00") if account.last_refresh else "",
        "refresh_token": account.refresh_token or "",
    }


def upload_to_cpa(
    token_data: dict,
    proxy: str = None,
    api_url: str = None,
    api_token: str = None,
) -> Tuple[bool, str]:
    """
    上传单个账号到 CPA 管理平台（不走代理）

    Args:
        token_data: Token JSON 数据
        proxy: 保留参数，不使用（CPA 上传始终直连）
        api_url: 指定 CPA API URL（优先于全局配置）
        api_token: 指定 CPA API Token（优先于全局配置）

    Returns:
        (成功标志, 消息或错误信息)
    """
    settings = get_settings()

    # 优先使用传入的参数，否则退回全局配置
    effective_url = api_url or settings.cpa_api_url
    effective_token = api_token or (settings.cpa_api_token.get_secret_value() if settings.cpa_api_token else "")

    # 仅当未指定服务时才检查全局启用开关
    if not api_url and not settings.cpa_enabled:
        return False, "CPA 上传未启用"

    if not effective_url:
        return False, "CPA API URL 未配置"

    if not effective_token:
        return False, "CPA API Token 未配置"

    upload_url = _normalize_cpa_auth_files_url(effective_url)

    filename = f"{token_data['email']}.json"
    file_content = json.dumps(token_data, ensure_ascii=False, indent=2).encode("utf-8")

    try:
        response = _post_cpa_auth_file_multipart(
            upload_url,
            filename,
            file_content,
            effective_token,
        )

        if response.status_code in (200, 201):
            return True, "上传成功"

        if response.status_code in (404, 405, 415):
            logger.warning("CPA multipart 上传失败，尝试原始 JSON 回退: %s", response.status_code)
            fallback_response = _post_cpa_auth_file_raw_json(
                upload_url,
                filename,
                file_content,
                effective_token,
            )
            if fallback_response.status_code in (200, 201):
                return True, "上传成功"
            response = fallback_response

        return False, _extract_cpa_error(response)

    except Exception as e:
        logger.error(f"CPA 上传异常: {e}")
        return False, f"上传异常: {str(e)}"


def batch_upload_to_cpa(
    account_ids: List[int],
    proxy: str = None,
    api_url: str = None,
    api_token: str = None,
) -> dict:
    """
    批量上传账号到 CPA 管理平台
    """
    results = {
        "success_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "details": []
    }

    import concurrent.futures
    import threading

    def _worker(account_id):
        # 1. 查询所需数据并关闭数据库连接
        with get_db() as db:
            account = db.query(Account).filter(Account.id == account_id).first()
            if not account:
                return "failed", account_id, None, "账号不存在"

            if not account.access_token:
                return "skipped", account_id, account.email, "缺少 Token"

            token_data = generate_token_json(account)
            email = account.email

        # 2. 在不占用数据库连接的情况下发起网络请求
        success, message = upload_to_cpa(token_data, proxy, api_url=api_url, api_token=api_token)

        # 3. 更新数据库
        if success:
            with get_db() as db:
                account = db.query(Account).filter(Account.id == account_id).first()
                if account:
                    account.cpa_uploaded = True
                    account.cpa_uploaded_at = datetime.utcnow()
                    db.commit()
            return "success", account_id, email, message
        else:
            return "failed", account_id, email, message

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(20, len(account_ids) if account_ids else 1)) as executor:
        futures = [executor.submit(_worker, acc_id) for acc_id in account_ids]
        for future in concurrent.futures.as_completed(futures):
            try:
                status, acc_id, email, msg = future.result()
                if status == "success":
                    results["success_count"] += 1
                    results["details"].append({"id": acc_id, "email": email, "success": True, "message": msg})
                elif status == "skipped":
                    results["skipped_count"] += 1
                    results["details"].append({"id": acc_id, "email": email, "success": False, "error": msg})
                else:
                    results["failed_count"] += 1
                    results["details"].append({"id": acc_id, "email": email, "success": False, "error": msg})
            except Exception as e:
                results["failed_count"] += 1
                logger.error(f"批量上传 CPA 发生异常: {e}")

    return results


def test_cpa_connection(api_url: str, api_token: str, proxy: str = None) -> Tuple[bool, str]:
    """
    测试 CPA 连接（不走代理）

    Args:
        api_url: CPA API URL
        api_token: CPA API Token
        proxy: 保留参数，不使用（CPA 始终直连）

    Returns:
        (成功标志, 消息)
    """
    if not api_url:
        return False, "API URL 不能为空"

    if not api_token:
        return False, "API Token 不能为空"

    test_url = _normalize_cpa_auth_files_url(api_url)
    headers = _build_cpa_headers(api_token)

    try:
        response = cffi_requests.get(
            test_url,
            headers=headers,
            proxies=None,
            timeout=10,
            impersonate="chrome110",
        )

        if response.status_code == 200:
            return True, "CPA 连接测试成功"
        if response.status_code == 401:
            return False, "连接成功，但 API Token 无效"
        if response.status_code == 403:
            return False, "连接成功，但服务端未启用远程管理或当前 Token 无权限"
        if response.status_code == 404:
            return False, "未找到 CPA auth-files 接口，请检查 API URL 是否填写为根地址、/v0/management 或完整 auth-files 地址"
        if response.status_code == 503:
            return False, "连接成功，但服务端认证管理器不可用"

        return False, f"服务器返回异常状态码: {response.status_code}"

    except cffi_requests.exceptions.ConnectionError as e:
        return False, f"无法连接到服务器: {str(e)}"
    except cffi_requests.exceptions.Timeout:
        return False, "连接超时，请检查网络配置"
    except Exception as e:
        return False, f"连接测试失败: {str(e)}"
