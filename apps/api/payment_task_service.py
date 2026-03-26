"""Shared bind-card task service for the migrated payment bridge."""

from datetime import datetime

from sqlalchemy import or_
from sqlalchemy.orm import joinedload

from src.database.models import Account, BindCardTask
from src.database.session import DatabaseSessionManager


class PaymentTaskService:
    """Small service layer for bind-card task CRUD behavior."""

    def __init__(self, database_url: str):
        self.manager = DatabaseSessionManager(database_url)
        self.manager.SessionLocal.configure(expire_on_commit=False)

    def create_task(
        self,
        request,
        *,
        resolve_proxy_fn,
        generate_checkout_link_fn,
        open_url_fn,
        serialize_task_fn,
        normalize_country_fn,
        normalize_currency_fn,
        official_checkout_check_fn,
    ):
        bind_mode = str(request.bind_mode or "semi_auto").strip().lower()
        if bind_mode not in ("semi_auto", "third_party", "local_auto"):
            raise ValueError("bind_mode 必须为 semi_auto / third_party / local_auto")

        with self.manager.session_scope() as db:
            account = db.query(Account).filter(Account.id == request.account_id).first()
            if not account:
                raise LookupError("账号不存在")

            proxy = resolve_proxy_fn(request.proxy, account)
            link, source, fallback_reason, checkout_session_id, publishable_key, client_secret = generate_checkout_link_fn(
                account=account,
                request=request,
                proxy=proxy,
            )

            task = BindCardTask(
                account_id=account.id,
                plan_type=request.plan_type,
                workspace_name=request.workspace_name if request.plan_type == "team" else None,
                price_interval=request.price_interval if request.plan_type == "team" else None,
                seat_quantity=request.seat_quantity if request.plan_type == "team" else None,
                country=normalize_country_fn(request.country),
                currency=normalize_currency_fn(normalize_country_fn(request.country), request.currency),
                checkout_url=link,
                checkout_session_id=checkout_session_id,
                publishable_key=publishable_key,
                client_secret=client_secret,
                checkout_source=source,
                bind_mode=bind_mode,
                status="link_ready",
            )
            db.add(task)
            db.commit()
            db.refresh(task)

            opened = False
            if request.auto_open and bind_mode == "semi_auto" and link:
                opened = open_url_fn(link, account.cookies if account else None)
                if opened:
                    task.status = "opened"
                    task.opened_at = datetime.utcnow()
                    db.commit()
                    db.refresh(task)

            return {
                "success": True,
                "task": serialize_task_fn(task),
                "link": link,
                "is_official_checkout": official_checkout_check_fn(link),
                "source": source,
                "fallback_reason": fallback_reason,
                "auto_opened": opened,
                "checkout_session_id": checkout_session_id,
                "publishable_key": publishable_key,
                "has_client_secret": bool(client_secret),
            }

    def list_tasks(self, *, page, page_size, status, search, serialize_task_fn):
        with self.manager.session_scope() as db:
            query = db.query(BindCardTask).options(joinedload(BindCardTask.account))
            if status:
                query = query.filter(BindCardTask.status == status)
            if search:
                pattern = f"%{search}%"
                query = query.join(Account, BindCardTask.account_id == Account.id).filter(
                    or_(Account.email.ilike(pattern), Account.account_id.ilike(pattern))
                )

            total = query.count()
            offset = (page - 1) * page_size
            tasks = query.order_by(BindCardTask.created_at.desc()).offset(offset).limit(page_size).all()

            now = datetime.utcnow()
            changed = False
            for task in tasks:
                account = task.account
                sub_type = str(getattr(account, "subscription_type", "") or "").lower()
                if sub_type in ("plus", "team") and task.status != "completed":
                    task.status = "completed"
                    task.completed_at = task.completed_at or getattr(account, "subscription_at", None) or now
                    task.last_error = None
                    task.last_checked_at = now
                    changed = True

            if changed:
                db.commit()

            return {
                "total": total,
                "tasks": [serialize_task_fn(task) for task in tasks],
            }

    def open_task(self, task_id: int, *, open_url_fn, serialize_task_fn):
        with self.manager.session_scope() as db:
            task = db.query(BindCardTask).options(joinedload(BindCardTask.account)).filter(BindCardTask.id == task_id).first()
            if not task:
                raise LookupError("绑卡任务不存在")
            if not task.checkout_url:
                raise ValueError("任务缺少 checkout 链接")

            cookies_str = task.account.cookies if task.account else None
            opened = open_url_fn(task.checkout_url, cookies_str)
            if opened:
                if str(task.status or "") not in ("paid_pending_sync", "completed"):
                    task.status = "opened"
                task.opened_at = datetime.utcnow()
                task.last_error = None
                db.commit()
                db.refresh(task)
                return {"success": True, "task": serialize_task_fn(task)}

            task.last_error = "未找到可用的浏览器"
            db.commit()
            raise RuntimeError("未找到可用的浏览器，请手动复制链接")

    def delete_task(self, task_id: int):
        with self.manager.session_scope() as db:
            task = db.query(BindCardTask).filter(BindCardTask.id == task_id).first()
            if not task:
                raise LookupError("绑卡任务不存在")
            db.delete(task)
            db.commit()
            return {"success": True, "task_id": task_id}
