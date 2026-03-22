"""
账号管理 API 路由
"""
import io
import json
import logging
import zipfile
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, BackgroundTasks, Body
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ...config.constants import AccountStatus
from ...config.settings import get_settings
from ...core.openai.token_refresh import refresh_account_token as do_refresh
from ...core.openai.token_refresh import validate_account_token as do_validate
from ...core.upload.cpa_upload import generate_token_json, batch_upload_to_cpa, upload_to_cpa
from ...core.upload.team_manager_upload import upload_to_team_manager, batch_upload_to_team_manager
from ...core.upload.sub2api_upload import batch_upload_to_sub2api, upload_to_sub2api

from ...core.dynamic_proxy import get_proxy_url_for_task
from ...database import crud
from ...database.models import Account
from ...database.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter()


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
    registered_at: Optional[str] = None
    last_refresh: Optional[str] = None
    expires_at: Optional[str] = None
    status: str
    proxy_used: Optional[str] = None
    cpa_uploaded: bool = False
    cpa_uploaded_at: Optional[str] = None
    primary_cpa_service_id: Optional[int] = None
    invalidated_at: Optional[str] = None
    invalid_reason: Optional[str] = None
    cookies: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    class Config:
        from_attributes = True


class AccountListResponse(BaseModel):
    """账号列表响应"""
    total: int
    accounts: List[AccountResponse]


class AccountUpdateRequest(BaseModel):
    """账号更新请求"""
    status: Optional[str] = None
    metadata: Optional[dict] = None
    cookies: Optional[str] = None  # 完整 cookie 字符串，用于支付请求


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
        query = query.filter(Account.status == status_filter)
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
        registered_at=account.registered_at.isoformat() if account.registered_at else None,
        last_refresh=account.last_refresh.isoformat() if account.last_refresh else None,
        expires_at=account.expires_at.isoformat() if account.expires_at else None,
        status=account.status,
        proxy_used=account.proxy_used,
        cpa_uploaded=account.cpa_uploaded or False,
        cpa_uploaded_at=account.cpa_uploaded_at.isoformat() if account.cpa_uploaded_at else None,
        primary_cpa_service_id=account.primary_cpa_service_id,
        invalidated_at=account.invalidated_at.isoformat() if account.invalidated_at else None,
        invalid_reason=account.invalid_reason,
        cookies=account.cookies,
        created_at=account.created_at.isoformat() if account.created_at else None,
        updated_at=account.updated_at.isoformat() if account.updated_at else None,
    )


# ============== API Endpoints ==============

@router.get("", response_model=AccountListResponse)
async def list_accounts(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    status: Optional[str] = Query(None, description="状态筛选"),
    email_service: Optional[str] = Query(None, description="邮箱服务筛选"),
    search: Optional[str] = Query(None, description="搜索关键词"),
    primary_cpa_service_id: Optional[int] = Query(None, description="主 CPA 服务 ID 筛选"),
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
            query = query.filter(Account.status == status)

        # 邮箱服务筛选
        if email_service:
            query = query.filter(Account.email_service == email_service)

        if primary_cpa_service_id is not None:
            query = query.filter(Account.primary_cpa_service_id == primary_cpa_service_id)

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

        return {
            "id": account.id,
            "email": account.email,
            "access_token": account.access_token,
            "refresh_token": account.refresh_token,
            "id_token": account.id_token,
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
            account.cpa_uploaded_at = datetime.utcnow()
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
            concurrency=concurrency, priority=priority
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
