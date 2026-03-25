import json
import time
import requests
import logging
from typing import List, Tuple

logger = logging.getLogger(__name__)

def upload_to_sync_manager(accounts: List[any], api_url: str, addr: str, api_token: str = "") -> Tuple[bool, str]:
    """
    同步账号到外部管理程序 (基于本地 RPC 接口)
    """
    if not accounts:
        return False, "没有账号需要同步"

    contents = []
    for account in accounts:
        # CodexManager 导入时，如果 tokens 节点存在，会从中读取 access_token 等
        # meta 节点用于存放 label, group_name 等界面展示信息
        acc_dict = {
            "email": account.email,
            "password": account.password,
            "meta": {
                "label": account.email,
                "group_name": "codex-console同步",
                "chatgpt_account_id": account.account_id,
                "workspace_id": account.workspace_id,
            },
            "tokens": {
                "access_token": account.access_token,
                "refresh_token": account.refresh_token,
                "id_token": account.id_token,
                "account_id": account.account_id,
            },
            "session_token": account.session_token or "",
            "email_service": account.email_service or "",
            "registered_at": account.created_at.isoformat() if account.created_at else "",
            "last_refresh": account.updated_at.isoformat() if account.updated_at else None,
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
    
    # 调试日志：查看生成的 payload 是否正确包含了 meta 信息
    logger.debug(f"Sync Payload (contents size): {len(contents)}")
    if contents:
        logger.debug(f"First item meta: {contents[0].get('meta', 'No meta found')}")

    # 为了兼容本地请求可能挂了系统代理导致连接失败的问题，这里添加 proxies 设置
    proxies = {
        "http": None,
        "https": None,
    }

    # 分析 API URL 判断目标是直连底层还是网关
    is_direct_rpc = api_url.endswith("/rpc") and not api_url.endswith("/api/rpc")

    if is_direct_rpc:
        # 这是直接发往后端 48760/rpc 的请求，会有严格的同源校验和 Token 校验
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": "http://127.0.0.1",
            "Sec-Fetch-Site": "same-origin"
        }
        
        # 即使前端没传，我们也尝试强制去读取系统的 Token，避免 401
        found_token = api_token
        
        if not found_token:
            # 尝试通过默认环境读取（如果 codex-console 和 codex-manager 跑在同一台电脑）
            import os
            try:
                app_data = os.getenv('APPDATA', '')
                local_app_data = os.getenv('LOCALAPPDATA', '')
                
                # Expand user profile to cover all bases
                user_profile = os.environ.get('USERPROFILE', r'C:\Users\Administrator')

                possible_paths = [
                    "codexmanager.rpc-token",
                    "../codexmanager.rpc-token",
                    "data/codexmanager.rpc-token",
                    # 补充绝对路径硬编码兜底
                    r"C:\Users\Administrator\AppData\Roaming\com.codexmanager.desktop\codexmanager.rpc-token",
                    r"C:\Users\Administrator\AppData\Local\com.codexmanager.desktop\codexmanager.rpc-token",
                    os.path.join(user_profile, r"AppData\Roaming\com.codexmanager.desktop\codexmanager.rpc-token"),
                    os.path.join(user_profile, r"AppData\Local\com.codexmanager.desktop\codexmanager.rpc-token")
                ]

                if app_data:
                    possible_paths.append(os.path.join(app_data, "com.codexmanager.desktop", "codexmanager.rpc-token"))
                    possible_paths.append(os.path.join(app_data, "CodexManager", "codexmanager.rpc-token"))      
                if local_app_data:
                    possible_paths.append(os.path.join(local_app_data, "com.codexmanager.desktop", "codexmanager.rpc-token"))
                    possible_paths.append(os.path.join(local_app_data, "CodexManager", "codexmanager.rpc-token"))

                for p in possible_paths:
                    if os.path.exists(p):
                        try:
                            with open(p, "r", encoding="utf-8") as f:
                                token_val = f.read().strip()
                                if token_val:
                                    found_token = token_val
                                    logger.info(f"成功从 {p} 读取到同步 Token")
                                    break
                        except Exception as e:
                            logger.error(f"读取 Token 文件 {p} 失败: {e}")
            except Exception as e:
                logger.error(f"寻找 Token 文件发生异常: {e}")
                
            # 再做一个终极兜底，如果找不到，尝试利用 CodexManager 安装目录
            if not found_token:
                try:
                    fallback_paths = [
                        r"D:\CodexManager\data\codexmanager.rpc-token",
                        r"C:\Program Files\CodexManager\data\codexmanager.rpc-token"
                    ]
                    for p in fallback_paths:
                        if os.path.exists(p):
                            with open(p, "r", encoding="utf-8") as f:
                                token_val = f.read().strip()
                                if token_val:
                                    found_token = token_val
                                    logger.info(f"成功从后备目录 {p} 读取到同步 Token")
                                    break
                except:
                    pass
                
        if found_token:
            headers["X-CodexManager-Rpc-Token"] = found_token
            logger.info(f"Using Token: {found_token[:5]}...")
        else:
            logger.error("未能在系统中找到 Token 文件！")
    else:
        # 这是发往前端网关 /api/rpc 的请求，不需要严格的 CORS 伪装，网关会自动附加 Token 转发给后端
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    try:
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
