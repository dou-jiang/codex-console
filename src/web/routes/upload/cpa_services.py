"""
CPA 服务管理 API 路由
"""

from typing import List, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ....database import crud
from ....database.session import get_db
from ....core.upload.cpa_upload import test_cpa_connection

router = APIRouter()


# ============== Pydantic Models ==============

class CpaServiceCreate(BaseModel):
    name: str
    api_url: str
    api_token: str
    proxy_url: Optional[str] = None
    enabled: bool = True
    priority: int = 0


class CpaServiceUpdate(BaseModel):
    name: Optional[str] = None
    api_url: Optional[str] = None
    api_token: Optional[str] = None
    proxy_url: Optional[str] = None
    enabled: Optional[bool] = None
    priority: Optional[int] = None


class CpaServiceResponse(BaseModel):
    id: int
    name: str
    api_url: str
    proxy_url: Optional[str] = None
    has_token: bool
    enabled: bool
    priority: int
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    class Config:
        from_attributes = True


class CpaServiceTestRequest(BaseModel):
    api_url: Optional[str] = None
    api_token: Optional[str] = None


def _to_response(svc) -> CpaServiceResponse:
    return CpaServiceResponse(
        id=svc.id,
        name=svc.name,
        api_url=svc.api_url,
        proxy_url=getattr(svc, "proxy_url", None),
        has_token=bool(svc.api_token),
        enabled=svc.enabled,
        priority=svc.priority,
        created_at=svc.created_at.isoformat() if svc.created_at else None,
        updated_at=svc.updated_at.isoformat() if svc.updated_at else None,
    )


# ============== API Endpoints ==============

@router.get("", response_model=List[CpaServiceResponse])
async def list_cpa_services(enabled: Optional[bool] = None):
    """获取 CPA 服务列表"""
    with get_db() as db:
        services = crud.get_cpa_services(db, enabled=enabled)
        return [_to_response(s) for s in services]


@router.post("", response_model=CpaServiceResponse)
async def create_cpa_service(request: CpaServiceCreate):
    """新增 CPA 服务"""
    with get_db() as db:
        service = crud.create_cpa_service(
            db,
            name=request.name,
            api_url=request.api_url,
            api_token=request.api_token,
            proxy_url=request.proxy_url,
            enabled=request.enabled,
            priority=request.priority,
        )
        return _to_response(service)


@router.get("/{service_id}", response_model=CpaServiceResponse)
async def get_cpa_service(service_id: int):
    """获取单个 CPA 服务详情"""
    with get_db() as db:
        service = crud.get_cpa_service_by_id(db, service_id)
        if not service:
            raise HTTPException(status_code=404, detail="CPA 服务不存在")
        return _to_response(service)


@router.get("/{service_id}/full")
async def get_cpa_service_full(service_id: int):
    """获取 CPA 服务完整配置（含 token）"""
    with get_db() as db:
        service = crud.get_cpa_service_by_id(db, service_id)
        if not service:
            raise HTTPException(status_code=404, detail="CPA 服务不存在")
        return {
            "id": service.id,
            "name": service.name,
            "api_url": service.api_url,
            "api_token": service.api_token,
            "proxy_url": getattr(service, "proxy_url", None),
            "enabled": service.enabled,
            "priority": service.priority,
        }


@router.patch("/{service_id}", response_model=CpaServiceResponse)
async def update_cpa_service(service_id: int, request: CpaServiceUpdate):
    """更新 CPA 服务配置"""
    with get_db() as db:
        service = crud.get_cpa_service_by_id(db, service_id)
        if not service:
            raise HTTPException(status_code=404, detail="CPA 服务不存在")

        update_data = {}
        if request.name is not None:
            update_data["name"] = request.name
        if request.api_url is not None:
            update_data["api_url"] = request.api_url
        # api_token 留空则保持原值
        if request.api_token:
            update_data["api_token"] = request.api_token
        if request.proxy_url is not None:
            update_data["proxy_url"] = request.proxy_url
        if request.enabled is not None:
            update_data["enabled"] = request.enabled
        if request.priority is not None:
            update_data["priority"] = request.priority

        service = crud.update_cpa_service(db, service_id, **update_data)
        return _to_response(service)


@router.delete("/{service_id}")
async def delete_cpa_service(service_id: int):
    """删除 CPA 服务"""
    with get_db() as db:
        service = crud.get_cpa_service_by_id(db, service_id)
        if not service:
            raise HTTPException(status_code=404, detail="CPA 服务不存在")
        crud.delete_cpa_service(db, service_id)
        return {"success": True, "message": f"CPA 服务 {service.name} 已删除"}


@router.post("/{service_id}/test")
async def test_cpa_service(service_id: int):
    """测试 CPA 服务连接"""
    with get_db() as db:
        service = crud.get_cpa_service_by_id(db, service_id)
        if not service:
            raise HTTPException(status_code=404, detail="CPA 服务不存在")
        success, message = test_cpa_connection(service.api_url, service.api_token)
        return {"success": success, "message": message}


@router.post("/test-connection")
async def test_cpa_connection_direct(request: CpaServiceTestRequest):
    """直接测试 CPA 连接（用于添加前验证）"""
    if not request.api_url or not request.api_token:
        raise HTTPException(status_code=400, detail="api_url 和 api_token 不能为空")
    success, message = test_cpa_connection(request.api_url, request.api_token)
    return {"success": success, "message": message}
