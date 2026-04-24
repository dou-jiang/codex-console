"""
Team Manager 服务管理 API 路由
"""

from typing import List, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from ....database import crud
from ....database.session import get_db
from ....core.upload.team_manager_upload import test_team_manager_connection

router = APIRouter()


# ============== Pydantic Models ==============

class TmServiceCreate(BaseModel):
    name: str
    api_url: str
    api_key: str
    enabled: bool = True
    priority: int = 0


class TmServiceUpdate(BaseModel):
    name: Optional[str] = None
    api_url: Optional[str] = None
    api_key: Optional[str] = None
    enabled: Optional[bool] = None
    priority: Optional[int] = None


class TmServiceResponse(BaseModel):
    id: int
    name: str
    api_url: str
    has_key: bool
    enabled: bool
    priority: int
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class TmTestRequest(BaseModel):
    api_url: Optional[str] = None
    api_key: Optional[str] = None


def _to_response(svc) -> TmServiceResponse:
    return TmServiceResponse(
        id=svc.id,
        name=svc.name,
        api_url=svc.api_url,
        has_key=bool(svc.api_key),
        enabled=svc.enabled,
        priority=svc.priority,
        created_at=svc.created_at.isoformat() if svc.created_at else None,
        updated_at=svc.updated_at.isoformat() if svc.updated_at else None,
    )


# ============== API Endpoints ==============

@router.get("", response_model=List[TmServiceResponse])
async def list_tm_services(enabled: Optional[bool] = None):
    """获取 Team Manager 服务列表"""
    with get_db() as db:
        services = crud.get_tm_services(db, enabled=enabled)
        return [_to_response(s) for s in services]


@router.post("", response_model=TmServiceResponse)
async def create_tm_service(request: TmServiceCreate):
    """新增 Team Manager 服务"""
    with get_db() as db:
        svc = crud.create_tm_service(
            db,
            name=request.name,
            api_url=request.api_url,
            api_key=request.api_key,
            enabled=request.enabled,
            priority=request.priority,
        )
        return _to_response(svc)


@router.get("/{service_id}", response_model=TmServiceResponse)
async def get_tm_service(service_id: int):
    """获取单个 Team Manager 服务详情"""
    with get_db() as db:
        svc = crud.get_tm_service_by_id(db, service_id)
        if not svc:
            raise HTTPException(status_code=404, detail="Team Manager 服务不存在")
        return _to_response(svc)


@router.patch("/{service_id}", response_model=TmServiceResponse)
async def update_tm_service(service_id: int, request: TmServiceUpdate):
    """更新 Team Manager 服务配置"""
    with get_db() as db:
        svc = crud.get_tm_service_by_id(db, service_id)
        if not svc:
            raise HTTPException(status_code=404, detail="Team Manager 服务不存在")

        update_data = {}
        if request.name is not None:
            update_data["name"] = request.name
        if request.api_url is not None:
            update_data["api_url"] = request.api_url
        if request.api_key:
            update_data["api_key"] = request.api_key
        if request.enabled is not None:
            update_data["enabled"] = request.enabled
        if request.priority is not None:
            update_data["priority"] = request.priority

        svc = crud.update_tm_service(db, service_id, **update_data)
        return _to_response(svc)


@router.delete("/{service_id}")
async def delete_tm_service(service_id: int):
    """删除 Team Manager 服务"""
    with get_db() as db:
        svc = crud.get_tm_service_by_id(db, service_id)
        if not svc:
            raise HTTPException(status_code=404, detail="Team Manager 服务不存在")
        crud.delete_tm_service(db, service_id)
        return {"success": True, "message": f"Team Manager 服务 {svc.name} 已删除"}


@router.post("/{service_id}/test")
async def test_tm_service(service_id: int):
    """测试 Team Manager 服务连接"""
    with get_db() as db:
        svc = crud.get_tm_service_by_id(db, service_id)
        if not svc:
            raise HTTPException(status_code=404, detail="Team Manager 服务不存在")
        success, message = test_team_manager_connection(svc.api_url, svc.api_key)
        return {"success": success, "message": message}


@router.post("/test-connection")
async def test_tm_connection_direct(request: TmTestRequest):
    """直接测试 Team Manager 连接（用于添加前验证）"""
    if not request.api_url or not request.api_key:
        raise HTTPException(status_code=400, detail="api_url 和 api_key 不能为空")
    success, message = test_team_manager_connection(request.api_url, request.api_key)
    return {"success": success, "message": message}
