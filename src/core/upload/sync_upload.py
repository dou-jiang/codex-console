import json
import time
import requests
import logging
from typing import List, Tuple

logger = logging.getLogger(__name__)

def upload_to_sync_manager(accounts: List[any], api_url: str, addr: str) -> Tuple[bool, str]:
    """
    同步账号到外部管理程序 (基于本地 RPC 接口)
    """
    if not accounts:
        return False, "没有账号需要同步"

    contents = []
    for account in accounts:
        acc_dict = {
            "email": account.email,
            "password": account.password,
            "client_id": account.client_id,
            "account_id": account.account_id,
            "workspace_id": account.workspace_id,
            "access_token": account.access_token,
            "refresh_token": account.refresh_token,
            "id_token": account.id_token,
            "session_token": account.session_token or "",
            "email_service": account.email_service or "",
            "registered_at": account.created_at.isoformat() if account.created_at else "",
            "last_refresh": account.updated_at.isoformat() if account.updated_at else None,
            "expires_at": None,
            "status": account.status or "active"
        }
        contents.append(acc_dict)

    # 封装成要求的 payload 格式
    # contents 是一个数组，内部是一个包含账号 JSON 字符串的数组（根据 curl 示例）
    payload = {
        "jsonrpc": "2.0",
        "id": int(time.time() * 1000),
        "method": "account/import",
        "params": {
            "addr": addr,
            "contents": [
                json.dumps(contents, ensure_ascii=False)
            ]
        }
    }

    headers = {
        "Content-Type": "application/json",
        "Accept": "*/*"
    }

    try:
        # 为了兼容本地请求可能挂了系统代理导致连接失败的问题，这里添加 proxies 设置
        proxies = {
            "http": None,
            "https": None,
        }
        
        response = requests.post(
            api_url,
            json=payload,
            headers=headers,
            timeout=15,
            proxies=proxies  # 强制绕过代理
        )
        response.raise_for_status()
        
        resp_data = response.json()
        if "error" in resp_data:
            return False, f"同步服务返回错误: {resp_data['error']}"
            
        return True, "同步成功"
    except requests.exceptions.RequestException as e:
        logger.error(f"同步请求失败: {e}")
        return False, f"请求失败: {e}"
    except Exception as e:
        logger.error(f"同步发生异常: {e}")
        return False, f"异常: {e}"
