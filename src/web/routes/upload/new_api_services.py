"""
new-api 服务管理 API 路由
"""

from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from ....core.upload.new_api_upload import batch_upload_to_new_api, test_new_api_connection
from ....database import crud
from ....database.session import get_db

router = APIRouter()


class NewApiServiceCreate(BaseModel):
    name: str
    api_url: str
    username: str
    password: str
    enabled: bool = True
    priority: int = 0


class NewApiServiceUpdate(BaseModel):
    name: Optional[str] = None
    api_url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    enabled: Optional[bool] = None
    priority: Optional[int] = None


class NewApiServiceResponse(BaseModel):
    id: int
    name: str
    api_url: str
    username: Optional[str] = None
    has_password: bool
    enabled: bool
    priority: int
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class NewApiTestRequest(BaseModel):
    api_url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None


class NewApiUploadRequest(BaseModel):
    account_ids: List[int]
    service_id: Optional[int] = None


def _to_response(service) -> NewApiServiceResponse:
    return NewApiServiceResponse(
        id=service.id,
        name=service.name,
        api_url=service.api_url,
        username=getattr(service, "username", None),
        has_password=bool(getattr(service, "password", None)),
        enabled=service.enabled,
        priority=service.priority,
        created_at=service.created_at.isoformat() if service.created_at else None,
        updated_at=service.updated_at.isoformat() if service.updated_at else None,
    )


@router.get("", response_model=List[NewApiServiceResponse])
async def list_new_api_services(enabled: Optional[bool] = None):
    """获取 new-api 服务列表。"""
    with get_db() as db:
        services = crud.get_new_api_services(db, enabled=enabled)
        return [_to_response(service) for service in services]


@router.post("", response_model=NewApiServiceResponse)
async def create_new_api_service(request: NewApiServiceCreate):
    """新增 new-api 服务。"""
    with get_db() as db:
        service = crud.create_new_api_service(
            db,
            name=request.name,
            api_url=request.api_url,
            username=request.username,
            password=request.password,
            enabled=request.enabled,
            priority=request.priority,
        )
        return _to_response(service)


@router.get("/{service_id}", response_model=NewApiServiceResponse)
async def get_new_api_service(service_id: int):
    """获取单个 new-api 服务详情。"""
    with get_db() as db:
        service = crud.get_new_api_service_by_id(db, service_id)
        if not service:
            raise HTTPException(status_code=404, detail="new-api 服务不存在")
        return _to_response(service)


@router.get("/{service_id}/full")
async def get_new_api_service_full(service_id: int):
    """获取 new-api 服务完整配置。"""
    with get_db() as db:
        service = crud.get_new_api_service_by_id(db, service_id)
        if not service:
            raise HTTPException(status_code=404, detail="new-api 服务不存在")
        return {
            "id": service.id,
            "name": service.name,
            "api_url": service.api_url,
            "username": getattr(service, "username", None),
            "password": getattr(service, "password", None),
            "enabled": service.enabled,
            "priority": service.priority,
        }


@router.patch("/{service_id}", response_model=NewApiServiceResponse)
async def update_new_api_service(service_id: int, request: NewApiServiceUpdate):
    """更新 new-api 服务配置。"""
    with get_db() as db:
        service = crud.get_new_api_service_by_id(db, service_id)
        if not service:
            raise HTTPException(status_code=404, detail="new-api 服务不存在")

        update_data = {}
        if request.name is not None:
            update_data["name"] = request.name
        if request.api_url is not None:
            update_data["api_url"] = request.api_url
        if request.username is not None:
            update_data["username"] = request.username
        if request.password:
            update_data["password"] = request.password
        if request.enabled is not None:
            update_data["enabled"] = request.enabled
        if request.priority is not None:
            update_data["priority"] = request.priority

        updated = crud.update_new_api_service(db, service_id, **update_data)
        return _to_response(updated)


@router.delete("/{service_id}")
async def delete_new_api_service(service_id: int):
    """删除 new-api 服务。"""
    with get_db() as db:
        service = crud.get_new_api_service_by_id(db, service_id)
        if not service:
            raise HTTPException(status_code=404, detail="new-api 服务不存在")
        crud.delete_new_api_service(db, service_id)
        return {"success": True, "message": f"new-api 服务 {service.name} 已删除"}


@router.post("/{service_id}/test")
async def test_new_api_service(service_id: int):
    """测试 new-api 服务连接。"""
    with get_db() as db:
        service = crud.get_new_api_service_by_id(db, service_id)
        if not service:
            raise HTTPException(status_code=404, detail="new-api 服务不存在")
        success, message = test_new_api_connection(service.api_url, getattr(service, 'username', None), getattr(service, 'password', None))
        return {"success": success, "message": message}


@router.post("/test-connection")
async def test_new_api_connection_direct(request: NewApiTestRequest):
    """直接测试 new-api 连接。"""
    if not request.api_url or not request.username or not request.password:
        raise HTTPException(status_code=400, detail="api_url、username 和 password 不能为空")
    success, message = test_new_api_connection(request.api_url, request.username, request.password)
    return {"success": success, "message": message}


@router.post("/upload")
async def upload_accounts_to_new_api(request: NewApiUploadRequest):
    """批量上传账号到 new-api 平台。"""
    if not request.account_ids:
        raise HTTPException(status_code=400, detail="账号 ID 列表不能为空")

    with get_db() as db:
        if request.service_id:
            service = crud.get_new_api_service_by_id(db, request.service_id)
        else:
            services = crud.get_new_api_services(db, enabled=True)
            service = services[0] if services else None

        if not service:
            raise HTTPException(status_code=400, detail="未找到可用的 new-api 服务")

    return batch_upload_to_new_api(request.account_ids, service.api_url, getattr(service, 'username', None), getattr(service, 'password', None))
