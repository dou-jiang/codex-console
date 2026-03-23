"""
支付相关 API 路由
"""

import logging
from typing import Optional, List
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...database.session import get_db
from ...database.models import Account
from ...database import crud
from ...config.settings import get_settings
from .accounts import resolve_account_ids, run_batch_concurrently
from ...core.openai.payment import (
    generate_plus_link,
    generate_team_link,
    open_url_incognito,
    check_subscription_status,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ============== Pydantic Models ==============

class GenerateLinkRequest(BaseModel):
    account_id: int
    plan_type: str  # 'plus' or 'team'
    workspace_name: str = "MyTeam"
    price_interval: str = "month"
    seat_quantity: int = 5
    proxy: Optional[str] = None
    auto_open: bool = False  # 生成后是否自动无痕打开
    country: str = "SG"  # 计费国家，决定货币  # 生成后是否自动无痕打开


class OpenIncognitoRequest(BaseModel):
    url: str
    account_id: Optional[int] = None  # 可选，用于注入账号 cookie


class MarkSubscriptionRequest(BaseModel):
    subscription_type: str  # 'free' / 'plus' / 'team'


class BatchCheckSubscriptionRequest(BaseModel):
    ids: List[int] = []
    proxy: Optional[str] = None
    concurrency: Optional[int] = 4
    select_all: bool = False
    status_filter: Optional[str] = None
    email_service_filter: Optional[str] = None
    search_filter: Optional[str] = None

# ============== 支付链接生成 ==============

@router.post("/generate-link")
def generate_payment_link(request: GenerateLinkRequest):
    """生成 Plus 或 Team 支付链接，可选自动无痕打开"""
    with get_db() as db:
        account = db.query(Account).filter(Account.id == request.account_id).first()
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")

        proxy = request.proxy or get_settings().proxy_url

        try:
            if request.plan_type == "plus":
                link = generate_plus_link(account, proxy, country=request.country)
            elif request.plan_type == "team":
                link = generate_team_link(
                    account,
                    workspace_name=request.workspace_name,
                    price_interval=request.price_interval,
                    seat_quantity=request.seat_quantity,
                    proxy=proxy,
                    country=request.country,
                )
            else:
                raise HTTPException(status_code=400, detail="plan_type 必须为 plus 或 team")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.error(f"生成支付链接失败: {e}")
            raise HTTPException(status_code=500, detail=f"生成链接失败: {str(e)}")

    opened = False
    if request.auto_open and link:
        cookies_str = account.cookies if account else None
        opened = open_url_incognito(link, cookies_str)

    return {
        "success": True,
        "link": link,
        "plan_type": request.plan_type,
        "auto_opened": opened,
    }


@router.post("/open-incognito")
def open_browser_incognito(request: OpenIncognitoRequest):
    """后端以无痕模式打开指定 URL，可注入账号 cookie"""
    if not request.url:
        raise HTTPException(status_code=400, detail="URL 不能为空")

    cookies_str = None
    if request.account_id:
        with get_db() as db:
            account = db.query(Account).filter(Account.id == request.account_id).first()
            if account:
                cookies_str = account.cookies

    success = open_url_incognito(request.url, cookies_str)
    if success:
        return {"success": True, "message": "已在无痕模式打开浏览器"}
    return {"success": False, "message": "未找到可用的浏览器，请手动复制链接"}


# ============== 订阅状态 ==============

def _check_subscription_for_account(account: Account, proxy: Optional[str], db) -> dict:
    logger.info(f"开始检测账号订阅状态: {account.email}")
    try:
        status = check_subscription_status(account, proxy)
        account.subscription_type = None if status == "free" else status
        account.subscription_at = datetime.utcnow() if status != "free" else account.subscription_at
        db.commit()
        logger.info(f"账号订阅检测成功: {account.email}, 结果: {status}")
        return {
            "id": account.id,
            "email": account.email,
            "success": True,
            "subscription_type": status,
        }
    except Exception as e:
        db.rollback()
        logger.warning(f"账号订阅检测失败: {account.email}, 原因: {e}")
        return {
            "id": account.id,
            "email": account.email,
            "success": False,
            "error": str(e),
        }


def _check_subscription_for_account_id(account_id: int, proxy: Optional[str]) -> dict:
    with get_db() as db:
        account = db.query(Account).filter(Account.id == account_id).first()
        if not account:
            return {"id": account_id, "email": None, "success": False, "error": "账号不存在"}

        return _check_subscription_for_account(account, proxy, db)


@router.post("/accounts/batch-check-subscription")
def batch_check_subscription(request: BatchCheckSubscriptionRequest):
    """批量检测账号订阅状态"""
    proxy = request.proxy or get_settings().proxy_url

    results = {"success_count": 0, "failed_count": 0, "details": []}

    with get_db() as db:
        ids = resolve_account_ids(
            db, request.ids, request.select_all,
            request.status_filter, request.email_service_filter, request.search_filter
        )

    def worker(account_id: int):
        return _check_subscription_for_account_id(account_id, proxy)

    for detail in run_batch_concurrently(ids, request.concurrency, worker):
        results["details"].append(detail)
        if detail["success"]:
            results["success_count"] += 1
        else:
            results["failed_count"] += 1

    return results


@router.post("/accounts/{account_id}/mark-subscription")
def mark_subscription(account_id: int, request: MarkSubscriptionRequest):
    """手动标记账号订阅类型"""
    allowed = ("free", "plus", "team")
    if request.subscription_type not in allowed:
        raise HTTPException(status_code=400, detail=f"subscription_type 必须为 {allowed}")

    with get_db() as db:
        account = db.query(Account).filter(Account.id == account_id).first()
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")

        account.subscription_type = None if request.subscription_type == "free" else request.subscription_type
        account.subscription_at = datetime.utcnow() if request.subscription_type != "free" else None
        db.commit()

    return {"success": True, "subscription_type": request.subscription_type}
