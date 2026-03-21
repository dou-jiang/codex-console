"""
设置 API 路由
"""

import asyncio
import logging
import os
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...config.settings import get_settings, update_settings
from ...core.ip_location import lookup_locations
from ...core.proxy_import import (
    allocate_proxy_names,
    proxy_host_port_key,
    collect_proxy_name_counters,
    iter_proxy_import_lines,
    parse_proxy_line,
)
from ...database import crud
from ...database.models import Proxy
from ...database.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter()


# ============== Pydantic Models ==============

class SettingItem(BaseModel):
    """设置项"""
    key: str
    value: str
    description: Optional[str] = None
    category: str = "general"


class SettingUpdateRequest(BaseModel):
    """设置更新请求"""
    value: str


class ProxySettings(BaseModel):
    """代理设置"""
    enabled: bool = False
    type: str = "http"  # http, socks5
    host: str = "127.0.0.1"
    port: int = 7890
    username: Optional[str] = None
    password: Optional[str] = None


class RegistrationSettings(BaseModel):
    """注册设置"""
    max_retries: int = 3
    timeout: int = 120
    default_password_length: int = 12
    sleep_min: int = 5
    sleep_max: int = 30


class WebUISettings(BaseModel):
    """Web UI 设置"""
    host: Optional[str] = None
    port: Optional[int] = None
    debug: Optional[bool] = None
    access_password: Optional[str] = None


class AllSettings(BaseModel):
    """所有设置"""
    proxy: ProxySettings
    registration: RegistrationSettings
    webui: WebUISettings


# ============== API Endpoints ==============

@router.get("")
async def get_all_settings():
    """获取所有设置"""
    settings = get_settings()

    return {
        "proxy": {
            "enabled": settings.proxy_enabled,
            "type": settings.proxy_type,
            "host": settings.proxy_host,
            "port": settings.proxy_port,
            "username": settings.proxy_username,
            "has_password": bool(settings.proxy_password),
            "dynamic_enabled": settings.proxy_dynamic_enabled,
            "dynamic_api_url": settings.proxy_dynamic_api_url,
            "dynamic_api_key_header": settings.proxy_dynamic_api_key_header,
            "dynamic_result_field": settings.proxy_dynamic_result_field,
            "has_dynamic_api_key": bool(settings.proxy_dynamic_api_key and settings.proxy_dynamic_api_key.get_secret_value()),
        },
        "registration": {
            "max_retries": settings.registration_max_retries,
            "timeout": settings.registration_timeout,
            "default_password_length": settings.registration_default_password_length,
            "sleep_min": settings.registration_sleep_min,
            "sleep_max": settings.registration_sleep_max,
        },
        "webui": {
            "host": settings.webui_host,
            "port": settings.webui_port,
            "debug": settings.debug,
            "has_access_password": bool(settings.webui_access_password and settings.webui_access_password.get_secret_value()),
        },
        "tempmail": {
            "base_url": settings.tempmail_base_url,
            "timeout": settings.tempmail_timeout,
            "max_retries": settings.tempmail_max_retries,
        },
        "email_code": {
            "timeout": settings.email_code_timeout,
            "poll_interval": settings.email_code_poll_interval,
        },
    }


@router.get("/proxy/dynamic")
async def get_dynamic_proxy_settings():
    """获取动态代理设置"""
    settings = get_settings()
    return {
        "enabled": settings.proxy_dynamic_enabled,
        "api_url": settings.proxy_dynamic_api_url,
        "api_key_header": settings.proxy_dynamic_api_key_header,
        "result_field": settings.proxy_dynamic_result_field,
        "has_api_key": bool(settings.proxy_dynamic_api_key and settings.proxy_dynamic_api_key.get_secret_value()),
    }


class DynamicProxySettings(BaseModel):
    """动态代理设置"""
    enabled: bool = False
    api_url: str = ""
    api_key: Optional[str] = None
    api_key_header: str = "X-API-Key"
    result_field: str = ""


@router.post("/proxy/dynamic")
async def update_dynamic_proxy_settings(request: DynamicProxySettings):
    """更新动态代理设置"""
    update_dict = {
        "proxy_dynamic_enabled": request.enabled,
        "proxy_dynamic_api_url": request.api_url,
        "proxy_dynamic_api_key_header": request.api_key_header,
        "proxy_dynamic_result_field": request.result_field,
    }
    if request.api_key is not None:
        update_dict["proxy_dynamic_api_key"] = request.api_key

    update_settings(**update_dict)
    return {"success": True, "message": "动态代理设置已更新"}


@router.post("/proxy/dynamic/test")
async def test_dynamic_proxy(request: DynamicProxySettings):
    """测试动态代理 API"""
    from ...core.dynamic_proxy import fetch_dynamic_proxy

    if not request.api_url:
        raise HTTPException(status_code=400, detail="请填写动态代理 API 地址")

    # 若未传入 api_key，使用已保存的
    api_key = request.api_key or ""
    if not api_key:
        settings = get_settings()
        if settings.proxy_dynamic_api_key:
            api_key = settings.proxy_dynamic_api_key.get_secret_value()

    proxy_url = fetch_dynamic_proxy(
        api_url=request.api_url,
        api_key=api_key,
        api_key_header=request.api_key_header,
        result_field=request.result_field,
    )

    if not proxy_url:
        return {"success": False, "message": "动态代理 API 返回为空或请求失败"}

    # 用获取到的代理测试连通性
    import time
    from curl_cffi import requests as cffi_requests
    try:
        proxies = {"http": proxy_url, "https": proxy_url}
        start = time.time()
        resp = cffi_requests.get(
            "https://api.ipify.org?format=json",
            proxies=proxies,
            timeout=10,
            impersonate="chrome110"
        )
        elapsed = round((time.time() - start) * 1000)
        if resp.status_code == 200:
            ip = resp.json().get("ip", "")
            return {"success": True, "proxy_url": proxy_url, "ip": ip, "response_time": elapsed,
                    "message": f"动态代理可用，出口 IP: {ip}，响应时间: {elapsed}ms"}
        return {"success": False, "proxy_url": proxy_url, "message": f"代理连接失败: HTTP {resp.status_code}"}
    except Exception as e:
        return {"success": False, "proxy_url": proxy_url, "message": f"代理连接失败: {e}"}


@router.get("/registration")
async def get_registration_settings():
    """获取注册设置"""
    settings = get_settings()

    return {
        "max_retries": settings.registration_max_retries,
        "timeout": settings.registration_timeout,
        "default_password_length": settings.registration_default_password_length,
        "sleep_min": settings.registration_sleep_min,
        "sleep_max": settings.registration_sleep_max,
    }


@router.post("/registration")
async def update_registration_settings(request: RegistrationSettings):
    """更新注册设置"""
    update_settings(
        registration_max_retries=request.max_retries,
        registration_timeout=request.timeout,
        registration_default_password_length=request.default_password_length,
        registration_sleep_min=request.sleep_min,
        registration_sleep_max=request.sleep_max,
    )

    return {"success": True, "message": "注册设置已更新"}


@router.post("/webui")
async def update_webui_settings(request: WebUISettings):
    """更新 Web UI 设置"""
    update_dict = {}
    if request.host is not None:
        update_dict["webui_host"] = request.host
    if request.port is not None:
        update_dict["webui_port"] = request.port
    if request.debug is not None:
        update_dict["debug"] = request.debug
    if request.access_password:
        update_dict["webui_access_password"] = request.access_password

    update_settings(**update_dict)
    return {"success": True, "message": "Web UI 设置已更新"}


@router.get("/database")
async def get_database_info():
    """获取数据库信息"""
    settings = get_settings()

    import os
    from pathlib import Path

    db_path = settings.database_url
    if db_path.startswith("sqlite:///"):
        db_path = db_path[10:]

    db_file = Path(db_path) if os.path.isabs(db_path) else Path(db_path)
    db_size = db_file.stat().st_size if db_file.exists() else 0

    with get_db() as db:
        from ...database.models import Account, EmailService, RegistrationTask

        account_count = db.query(Account).count()
        service_count = db.query(EmailService).count()
        task_count = db.query(RegistrationTask).count()

    return {
        "database_url": settings.database_url,
        "database_size_bytes": db_size,
        "database_size_mb": round(db_size / (1024 * 1024), 2),
        "accounts_count": account_count,
        "email_services_count": service_count,
        "tasks_count": task_count,
    }


@router.post("/database/backup")
async def backup_database():
    """备份数据库"""
    import shutil
    from datetime import datetime

    settings = get_settings()

    db_path = settings.database_url
    if db_path.startswith("sqlite:///"):
        db_path = db_path[10:]

    if not os.path.exists(db_path):
        raise HTTPException(status_code=404, detail="数据库文件不存在")

    # 创建备份目录
    from pathlib import Path as FilePath
    backup_dir = FilePath(db_path).parent / "backups"
    backup_dir.mkdir(exist_ok=True)

    # 生成备份文件名
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"database_backup_{timestamp}.db"

    # 复制数据库文件
    shutil.copy2(db_path, backup_path)

    return {
        "success": True,
        "message": "数据库备份成功",
        "backup_path": str(backup_path)
    }


@router.post("/database/cleanup")
async def cleanup_database(
    days: int = 30,
    keep_failed: bool = True
):
    """清理过期数据"""
    from datetime import datetime, timedelta

    cutoff_date = datetime.utcnow() - timedelta(days=days)

    with get_db() as db:
        from ...database.models import RegistrationTask
        from sqlalchemy import delete

        # 删除旧任务
        conditions = [RegistrationTask.created_at < cutoff_date]
        if not keep_failed:
            conditions.append(RegistrationTask.status != "failed")
        else:
            conditions.append(RegistrationTask.status.in_(["completed", "cancelled"]))

        result = db.execute(
            delete(RegistrationTask).where(*conditions)
        )
        db.commit()

        deleted_count = result.rowcount

    return {
        "success": True,
        "message": f"已清理 {deleted_count} 条过期任务记录",
        "deleted_count": deleted_count
    }


@router.get("/logs")
async def get_recent_logs(
    lines: int = 100,
    level: str = "INFO"
):
    """获取最近日志"""
    settings = get_settings()

    log_file = settings.log_file
    if not log_file:
        return {"logs": [], "message": "日志文件未配置"}

    from pathlib import Path
    log_path = Path(log_file)

    if not log_path.exists():
        return {"logs": [], "message": "日志文件不存在"}

    try:
        with open(log_path, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
            recent_lines = all_lines[-lines:]

        return {
            "logs": [line.strip() for line in recent_lines],
            "total_lines": len(all_lines)
        }
    except Exception as e:
        return {"logs": [], "error": str(e)}


# ============== 临时邮箱设置 ==============

class TempmailSettings(BaseModel):
    """临时邮箱设置"""
    api_url: Optional[str] = None
    enabled: bool = True


class EmailCodeSettings(BaseModel):
    """验证码等待设置"""
    timeout: int = 120  # 验证码等待超时（秒）
    poll_interval: int = 3  # 验证码轮询间隔（秒）


@router.get("/tempmail")
async def get_tempmail_settings():
    """获取临时邮箱设置"""
    settings = get_settings()

    return {
        "api_url": settings.tempmail_base_url,
        "timeout": settings.tempmail_timeout,
        "max_retries": settings.tempmail_max_retries,
        "enabled": True  # 临时邮箱默认可用
    }


@router.post("/tempmail")
async def update_tempmail_settings(request: TempmailSettings):
    """更新临时邮箱设置"""
    update_dict = {}

    if request.api_url:
        update_dict["tempmail_base_url"] = request.api_url

    update_settings(**update_dict)

    return {"success": True, "message": "临时邮箱设置已更新"}


# ============== 验证码等待设置 ==============

@router.get("/email-code")
async def get_email_code_settings():
    """获取验证码等待设置"""
    settings = get_settings()
    return {
        "timeout": settings.email_code_timeout,
        "poll_interval": settings.email_code_poll_interval,
    }


@router.post("/email-code")
async def update_email_code_settings(request: EmailCodeSettings):
    """更新验证码等待设置"""
    # 验证参数范围
    if request.timeout < 30 or request.timeout > 600:
        raise HTTPException(status_code=400, detail="超时时间必须在 30-600 秒之间")
    if request.poll_interval < 1 or request.poll_interval > 30:
        raise HTTPException(status_code=400, detail="轮询间隔必须在 1-30 秒之间")

    update_settings(
        email_code_timeout=request.timeout,
        email_code_poll_interval=request.poll_interval,
    )

    return {"success": True, "message": "验证码等待设置已更新"}


# ============== 代理列表 CRUD ==============

class ProxyCreateRequest(BaseModel):
    """创建代理请求"""
    name: str
    type: str = "http"  # http, socks5
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    enabled: bool = True
    priority: int = 0


class ProxyUpdateRequest(BaseModel):
    """更新代理请求"""
    name: Optional[str] = None
    type: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None
    enabled: Optional[bool] = None
    priority: Optional[int] = None


class ProxyBatchImportRequest(BaseModel):
    """批量导入代理请求"""
    data: str
    default_type: Literal["http", "socks5"] = "http"


class ProxyBatchDeleteRequest(BaseModel):
    """批量删除代理请求"""
    ids: list[int]


@router.get("/proxies")
async def get_proxies_list(
    enabled: Optional[bool] = None,
    keyword: Optional[str] = None,
    type: Optional[str] = None,
    is_default: Optional[bool] = None,
    location: Optional[str] = None,
):
    """获取代理列表"""
    with get_db() as db:
        proxies = crud.get_proxies(
            db,
            enabled=enabled,
            keyword=keyword,
            type=type,
            is_default=is_default,
            location=location,
        )
        return {
            "proxies": [p.to_dict() for p in proxies],
            "total": len(proxies)
        }


@router.post("/proxies")
async def create_proxy_item(request: ProxyCreateRequest):
    """创建代理"""
    with get_db() as db:
        proxy = crud.create_proxy(
            db,
            name=request.name,
            type=request.type,
            host=request.host,
            port=request.port,
            username=request.username,
            password=request.password,
            enabled=request.enabled,
            priority=request.priority
        )
        return {"success": True, "proxy": proxy.to_dict()}


@router.post("/proxies/batch-import")
async def batch_import_proxies(request: ProxyBatchImportRequest):
    """批量导入代理"""
    import_candidates = []
    results = []
    skipped = 0
    failed = 0
    pending_host_ports: set[tuple[str, int]] = set()

    with get_db() as db:
        existing_host_ports = {
            proxy_host_port_key(host, port)
            for host, port in db.query(Proxy.host, Proxy.port).all()
        }

        for line_no, raw_line in iter_proxy_import_lines(request.data):
            try:
                parsed = parse_proxy_line(raw_line, request.default_type, line_no)
            except ValueError as exc:
                failed += 1
                results.append({
                    "line_no": line_no,
                    "raw": raw_line,
                    "status": "failed",
                    "reason": str(exc),
                })
                continue

            host_port = proxy_host_port_key(parsed.host, parsed.port)
            if host_port in pending_host_ports or host_port in existing_host_ports:
                skipped += 1
                results.append({
                    "line_no": line_no,
                    "raw": raw_line,
                    "status": "skipped",
                    "reason": "duplicate",
                })
                continue

            pending_host_ports.add(host_port)
            import_candidates.append(parsed)

        if not import_candidates:
            return {
                "success": 0,
                "skipped": skipped,
                "failed": failed,
                "results": results,
            }

    try:
        locations_by_host = await asyncio.to_thread(
            lookup_locations,
            [item.host for item in import_candidates],
        )
    except Exception:
        locations_by_host = {}

    with get_db() as db:
        existing_names = [row[0] for row in db.query(Proxy.name).all()]
        existing_host_ports = {
            proxy_host_port_key(host, port)
            for host, port in db.query(Proxy.host, Proxy.port).all()
        }
        prefixes_in_db = collect_proxy_name_counters(existing_names)
        allocated_names = allocate_proxy_names(
            prefixes_in_db,
            [
                {
                    "key": f"{item.line_no}:{item.host}:{item.port}",
                    "host": item.host,
                    "country": locations_by_host.get(item.host).country if locations_by_host.get(item.host) else "",
                    "city": locations_by_host.get(item.host).city if locations_by_host.get(item.host) else "",
                }
                for item in import_candidates
            ],
        )

        success = 0
        seen_host_ports: set[tuple[str, int]] = set()
        for item in import_candidates:
            key = proxy_host_port_key(item.host, item.port)
            if key in seen_host_ports or key in existing_host_ports:
                skipped += 1
                results.append({
                    "line_no": item.line_no,
                    "raw": item.raw_line,
                    "status": "skipped",
                    "reason": "duplicate",
                })
                continue

            seen_host_ports.add(key)
            location = locations_by_host.get(item.host)
            proxy = crud.create_proxy(
                db,
                name=allocated_names.get(f"{item.line_no}:{item.host}:{item.port}", f"代理-{success + 1:03d}"),
                type=item.type,
                host=item.host,
                port=item.port,
                username=item.username,
                password=item.password,
                country=location.country if location else None,
                city=location.city if location else None,
            )
            success += 1
            results.append({
                "line_no": item.line_no,
                "raw": item.raw_line,
                "status": "success",
                "proxy": proxy.to_dict(),
            })

    return {
        "success": success,
        "skipped": skipped,
        "failed": failed,
        "results": results,
    }


@router.post("/proxies/batch-delete")
async def batch_delete_proxies(request: ProxyBatchDeleteRequest):
    """批量删除代理"""
    with get_db() as db:
        existing_ids = {row[0] for row in db.query(Proxy.id).filter(Proxy.id.in_(request.ids)).all()}
        deleted = crud.delete_proxies(db, request.ids)
        missing = [proxy_id for proxy_id in request.ids if proxy_id not in existing_ids]

    return {
        "success": True,
        "requested": len(request.ids),
        "deleted": deleted,
        "missing": missing,
    }


@router.get("/proxies/{proxy_id}")
async def get_proxy_item(proxy_id: int):
    """获取单个代理"""
    with get_db() as db:
        proxy = crud.get_proxy_by_id(db, proxy_id)
        if not proxy:
            raise HTTPException(status_code=404, detail="代理不存在")
        return proxy.to_dict(include_password=True)


@router.patch("/proxies/{proxy_id}")
async def update_proxy_item(proxy_id: int, request: ProxyUpdateRequest):
    """更新代理"""
    with get_db() as db:
        update_data = {}
        if request.name is not None:
            update_data["name"] = request.name
        if request.type is not None:
            update_data["type"] = request.type
        if request.host is not None:
            update_data["host"] = request.host
        if request.port is not None:
            update_data["port"] = request.port
        if request.username is not None:
            update_data["username"] = request.username
        if request.password is not None:
            update_data["password"] = request.password
        if request.enabled is not None:
            update_data["enabled"] = request.enabled
        if request.priority is not None:
            update_data["priority"] = request.priority

        proxy = crud.update_proxy(db, proxy_id, **update_data)
        if not proxy:
            raise HTTPException(status_code=404, detail="代理不存在")
        return {"success": True, "proxy": proxy.to_dict()}


@router.delete("/proxies/{proxy_id}")
async def delete_proxy_item(proxy_id: int):
    """删除代理"""
    with get_db() as db:
        success = crud.delete_proxy(db, proxy_id)
        if not success:
            raise HTTPException(status_code=404, detail="代理不存在")
        return {"success": True, "message": "代理已删除"}


@router.post("/proxies/{proxy_id}/set-default")
async def set_proxy_default(proxy_id: int):
    """将指定代理设为默认"""
    with get_db() as db:
        proxy = crud.set_proxy_default(db, proxy_id)
        if not proxy:
            raise HTTPException(status_code=404, detail="代理不存在")
        return {"success": True, "proxy": proxy.to_dict()}


@router.post("/proxies/{proxy_id}/test")
async def test_proxy_item(proxy_id: int):
    """测试单个代理"""
    import time
    from curl_cffi import requests as cffi_requests

    with get_db() as db:
        proxy = crud.get_proxy_by_id(db, proxy_id)
        if not proxy:
            raise HTTPException(status_code=404, detail="代理不存在")

        proxy_url = proxy.proxy_url
        test_url = "https://api.ipify.org?format=json"
        start_time = time.time()

        try:
            proxies = {
                "http": proxy_url,
                "https": proxy_url
            }

            response = cffi_requests.get(
                test_url,
                proxies=proxies,
                timeout=3,
                impersonate="chrome110"
            )

            elapsed_time = time.time() - start_time

            if response.status_code == 200:
                ip_info = response.json()
                return {
                    "success": True,
                    "ip": ip_info.get("ip", ""),
                    "response_time": round(elapsed_time * 1000),
                    "message": f"代理连接成功，出口 IP: {ip_info.get('ip', 'unknown')}"
                }
            else:
                return {
                    "success": False,
                    "message": f"代理返回错误状态码: {response.status_code}"
                }

        except Exception as e:
            return {
                "success": False,
                "message": f"代理连接失败: {str(e)}"
            }


@router.post("/proxies/test-all")
async def test_all_proxies():
    """测试所有启用的代理"""
    import time
    from curl_cffi import requests as cffi_requests

    with get_db() as db:
        proxies = crud.get_enabled_proxies(db)

        results = []
        for proxy in proxies:
            proxy_url = proxy.proxy_url
            test_url = "https://api.ipify.org?format=json"
            start_time = time.time()

            try:
                proxies_dict = {
                    "http": proxy_url,
                    "https": proxy_url
                }

                response = cffi_requests.get(
                    test_url,
                    proxies=proxies_dict,
                    timeout=3,
                    impersonate="chrome110"
                )

                elapsed_time = time.time() - start_time

                if response.status_code == 200:
                    ip_info = response.json()
                    results.append({
                        "id": proxy.id,
                        "name": proxy.name,
                        "success": True,
                        "ip": ip_info.get("ip", ""),
                        "response_time": round(elapsed_time * 1000)
                    })
                else:
                    results.append({
                        "id": proxy.id,
                        "name": proxy.name,
                        "success": False,
                        "message": f"状态码: {response.status_code}"
                    })

            except Exception as e:
                results.append({
                    "id": proxy.id,
                    "name": proxy.name,
                    "success": False,
                    "message": str(e)
                })

        success_count = sum(1 for r in results if r["success"])
        return {
            "total": len(proxies),
            "success": success_count,
            "failed": len(proxies) - success_count,
            "results": results
        }


@router.post("/proxies/{proxy_id}/enable")
async def enable_proxy(proxy_id: int):
    """启用代理"""
    with get_db() as db:
        proxy = crud.update_proxy(db, proxy_id, enabled=True)
        if not proxy:
            raise HTTPException(status_code=404, detail="代理不存在")
        return {"success": True, "message": "代理已启用"}


@router.post("/proxies/{proxy_id}/disable")
async def disable_proxy(proxy_id: int):
    """禁用代理"""
    with get_db() as db:
        proxy = crud.update_proxy(db, proxy_id, enabled=False)
        if not proxy:
            raise HTTPException(status_code=404, detail="代理不存在")
        return {"success": True, "message": "代理已禁用"}


# ============== Outlook 设置 ==============

class OutlookSettings(BaseModel):
    """Outlook 设置"""
    default_client_id: Optional[str] = None


@router.get("/outlook")
async def get_outlook_settings():
    """获取 Outlook 设置"""
    settings = get_settings()

    return {
        "default_client_id": settings.outlook_default_client_id,
        "provider_priority": settings.outlook_provider_priority,
        "health_failure_threshold": settings.outlook_health_failure_threshold,
        "health_disable_duration": settings.outlook_health_disable_duration,
    }


@router.post("/outlook")
async def update_outlook_settings(request: OutlookSettings):
    """更新 Outlook 设置"""
    update_dict = {}

    if request.default_client_id is not None:
        update_dict["outlook_default_client_id"] = request.default_client_id

    if update_dict:
        update_settings(**update_dict)

    return {"success": True, "message": "Outlook 设置已更新"}


# ============== Team Manager 设置 ==============

class TeamManagerSettings(BaseModel):
    """Team Manager 设置"""
    enabled: bool = False
    api_url: str = ""
    api_key: str = ""


class TeamManagerTestRequest(BaseModel):
    """Team Manager 测试请求"""
    api_url: str
    api_key: str


@router.get("/team-manager")
async def get_team_manager_settings():
    """获取 Team Manager 设置"""
    settings = get_settings()
    return {
        "enabled": settings.tm_enabled,
        "api_url": settings.tm_api_url,
        "has_api_key": bool(settings.tm_api_key and settings.tm_api_key.get_secret_value()),
    }


@router.post("/team-manager")
async def update_team_manager_settings(request: TeamManagerSettings):
    """更新 Team Manager 设置"""
    update_dict = {
        "tm_enabled": request.enabled,
        "tm_api_url": request.api_url,
    }
    if request.api_key:
        update_dict["tm_api_key"] = request.api_key
    update_settings(**update_dict)
    return {"success": True, "message": "Team Manager 设置已更新"}


@router.post("/team-manager/test")
async def test_team_manager_connection(request: TeamManagerTestRequest):
    """测试 Team Manager 连接"""
    from ...core.upload.team_manager_upload import test_team_manager_connection as do_test

    settings = get_settings()
    api_key = request.api_key
    if api_key == 'use_saved_key' or not api_key:
        if settings.tm_api_key:
            api_key = settings.tm_api_key.get_secret_value()
        else:
            return {"success": False, "message": "未配置 API Key"}

    success, message = do_test(request.api_url, api_key)
    return {"success": success, "message": message}
