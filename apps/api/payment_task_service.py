"""Shared bind-card task service for the migrated payment bridge."""

from datetime import datetime
import time

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

    def sync_subscription(self, task_id: int, request, **kwargs):
        resolve_proxy_fn = kwargs["resolve_proxy_fn"]
        check_subscription_fn = kwargs["check_subscription_fn"]
        serialize_task_fn = kwargs["serialize_task_fn"]

        with self.manager.session_scope() as db:
            task = db.query(BindCardTask).options(joinedload(BindCardTask.account)).filter(BindCardTask.id == task_id).first()
            if not task:
                raise LookupError("绑卡任务不存在")
            account = task.account
            if not account:
                raise LookupError("任务关联账号不存在")

            proxy = resolve_proxy_fn(request.proxy, account)
            now = datetime.utcnow()
            try:
                detail, refreshed = check_subscription_fn(
                    db=db,
                    account=account,
                    proxy=proxy,
                    allow_token_refresh=True,
                )
                status = str(detail.get("status") or "free").lower()
            except Exception as exc:
                task.status = "failed"
                task.last_error = str(exc)
                task.last_checked_at = now
                db.commit()
                raise RuntimeError(f"订阅检测失败: {exc}")

            if status in ("plus", "team"):
                account.subscription_type = status
                account.subscription_at = now
            elif status == "free" and str(detail.get("confidence") or "").lower() == "high":
                account.subscription_type = None
                account.subscription_at = None

            task.last_checked_at = now
            if status in ("plus", "team"):
                task.status = "completed"
                task.completed_at = now
                task.last_error = None
            else:
                task.completed_at = None
                task.status = "waiting_user_action"

            db.commit()
            db.refresh(task)
            return {
                "success": True,
                "subscription_type": status,
                "detail": detail,
                "task": serialize_task_fn(task),
                "account_id": account.id,
                "account_email": account.email,
            }

    def mark_user_action(self, task_id: int, request, **kwargs):
        resolve_proxy_fn = kwargs["resolve_proxy_fn"]
        check_subscription_fn = kwargs["check_subscription_fn"]
        serialize_task_fn = kwargs["serialize_task_fn"]

        with self.manager.session_scope() as db:
            task = db.query(BindCardTask).options(joinedload(BindCardTask.account)).filter(BindCardTask.id == task_id).first()
            if not task:
                raise LookupError("绑卡任务不存在")
            account = task.account
            if not account:
                raise LookupError("任务关联账号不存在")

            proxy = resolve_proxy_fn(request.proxy, account)
            timeout_seconds = int(request.timeout_seconds)
            interval_seconds = int(request.interval_seconds)

            previous_status = str(task.status or "")
            now = datetime.utcnow()
            task.status = "verifying"
            task.last_error = None
            task.last_checked_at = now
            db.commit()

            deadline = time.monotonic() + timeout_seconds
            checks = 0
            last_status = "free"
            token_refresh_used = False
            detail = None

            while time.monotonic() < deadline:
                checks += 1
                try:
                    detail, refreshed = check_subscription_fn(
                        db=db,
                        account=account,
                        proxy=proxy,
                        allow_token_refresh=not token_refresh_used,
                    )
                    if refreshed:
                        token_refresh_used = True
                    status = str(detail.get("status") or "free").lower()
                    last_status = status
                except Exception as exc:
                    failed_at = datetime.utcnow()
                    task.status = "failed"
                    task.last_error = f"订阅检测失败: {exc}"
                    task.last_checked_at = failed_at
                    db.commit()
                    raise RuntimeError(f"订阅检测失败: {exc}")

                checked_at = datetime.utcnow()
                if status in ("plus", "team"):
                    account.subscription_type = status
                    account.subscription_at = checked_at
                elif status == "free" and str(detail.get("confidence") or "").lower() == "high":
                    account.subscription_type = None
                    account.subscription_at = None
                task.last_checked_at = checked_at

                if status in ("plus", "team"):
                    task.status = "completed"
                    task.completed_at = checked_at
                    task.last_error = None
                    db.commit()
                    db.refresh(task)
                    return {
                        "success": True,
                        "verified": True,
                        "checks": checks,
                        "subscription_type": status,
                        "detail": detail,
                        "token_refresh_used": token_refresh_used,
                        "task": serialize_task_fn(task),
                        "account_id": account.id,
                        "account_email": account.email,
                    }

                db.commit()
                if time.monotonic() + interval_seconds >= deadline:
                    break
                time.sleep(interval_seconds)

            timeout_confidence = str((detail or {}).get("confidence") or "unknown").lower()
            if previous_status == "paid_pending_sync" or (last_status == "free" and timeout_confidence != "high"):
                task.status = "paid_pending_sync"
            else:
                task.status = "waiting_user_action"
            task.last_checked_at = datetime.utcnow()
            task.completed_at = None
            db.commit()
            db.refresh(task)

            return {
                "success": True,
                "verified": False,
                "checks": checks,
                "subscription_type": last_status,
                "detail": detail,
                "token_refresh_used": token_refresh_used,
                "task": serialize_task_fn(task),
                "account_id": account.id,
                "account_email": account.email,
            }

    def auto_bind_third_party(self, task_id: int, request, **kwargs):
        serialize_task_fn = kwargs["serialize_task_fn"]
        resolve_proxy_fn = kwargs["resolve_proxy_fn"]
        resolve_api_url_fn = kwargs["resolve_api_url_fn"]
        resolve_api_key_fn = kwargs["resolve_api_key_fn"]
        invoke_api_fn = kwargs["invoke_api_fn"]
        sanitize_response_fn = kwargs["sanitize_response_fn"]
        assess_submission_fn = kwargs["assess_submission_fn"]
        challenge_pending_fn = kwargs["challenge_pending_fn"]
        poll_status_fn = kwargs["poll_status_fn"]
        mark_paid_pending_fn = kwargs["mark_paid_pending_fn"]
        extract_checkout_session_id_fn = kwargs["extract_checkout_session_id_fn"]

        third_party_response_safe: dict = {}
        third_party_assessment: dict = {}
        third_party_status_poll: dict = {}

        with self.manager.session_scope() as db:
            task = db.query(BindCardTask).options(joinedload(BindCardTask.account)).filter(BindCardTask.id == task_id).first()
            if not task:
                raise LookupError("绑卡任务不存在")
            account = task.account
            if not account:
                raise LookupError("任务关联账号不存在")

            checkout_session_id = str(task.checkout_session_id or "").strip() or extract_checkout_session_id_fn(task.checkout_url)
            publishable_key = str(task.publishable_key or "").strip()
            if not checkout_session_id:
                raise ValueError("任务缺少 checkout_session_id，请重新创建任务")
            if not publishable_key:
                raise ValueError("任务缺少 publishable_key，请重新创建任务")

            api_url = resolve_api_url_fn(request.api_url)
            api_key = resolve_api_key_fn(request.api_key)
            if not api_url:
                raise ValueError("缺少第三方 API 地址")

            proxy = resolve_proxy_fn(request.proxy, account)
            payload = {
                "checkout_session_id": checkout_session_id,
                "publishable_key": publishable_key,
                "client_secret": str(getattr(task, "client_secret", "") or "").strip() or None,
                "checkout_url": str(task.checkout_url or "").strip() or None,
                "plan_type": str(task.plan_type or "").strip().lower(),
                "country": "US",
                "currency": "USD",
                "card": {
                    "number": str(request.card.number or "").strip(),
                    "exp_month": str(request.card.exp_month or "").strip().zfill(2),
                    "exp_year": str(request.card.exp_year or "").strip()[-2:],
                    "cvc": str(request.card.cvc or "").strip(),
                },
                "profile": {
                    "name": str(request.profile.name or "").strip(),
                    "email": str(request.profile.email or account.email or "").strip(),
                    "country": str(request.profile.country or "US").strip().upper(),
                    "line1": str(request.profile.line1 or "").strip(),
                    "city": str(request.profile.city or "").strip(),
                    "state": str(request.profile.state or "").strip(),
                    "postal": str(request.profile.postal or "").strip(),
                },
            }

            if not payload["card"]["number"] or not payload["card"]["cvc"]:
                raise ValueError("卡号/CVC 不能为空")

            task.bind_mode = "third_party"
            task.status = "verifying"
            task.last_error = None
            task.last_checked_at = datetime.utcnow()
            db.commit()

            try:
                third_party_response, used_endpoint = invoke_api_fn(
                    api_url=api_url,
                    api_key=api_key,
                    payload=payload,
                    proxy=proxy,
                )
                third_party_response_safe = sanitize_response_fn(third_party_response)
                third_party_assessment = assess_submission_fn(third_party_response)
                assess_state = str(third_party_assessment.get("state") or "pending").lower()
                assess_reason = str(third_party_assessment.get("reason") or "").strip()
                assess_snapshot = third_party_assessment.get("snapshot") if isinstance(third_party_assessment, dict) else {}
                payment_status = str((assess_snapshot or {}).get("payment_status") or "").lower()
            except Exception as exc:
                task.status = "failed"
                task.last_error = f"第三方绑卡提交失败: {exc}"
                task.last_checked_at = datetime.utcnow()
                db.commit()
                raise RuntimeError(str(exc))

            if assess_state == "failed":
                task.status = "failed"
                task.last_error = f"第三方返回失败: {assess_reason or 'unknown'}"
                task.last_checked_at = datetime.utcnow()
                db.commit()
                raise ValueError(f"第三方返回失败: {assess_reason or 'unknown'}")

            paid_hint_status = payment_status or "paid"
            paid_hint_reason = assess_reason or "third_party_paid_signal"

            if assess_state == "pending":
                if challenge_pending_fn(third_party_assessment):
                    task.status = "waiting_user_action"
                    task.last_checked_at = datetime.utcnow()
                    hint_reason = assess_reason or "requires_action"
                    hint_payment_status = payment_status or "unknown"
                    task.last_error = (
                        f"第三方已受理并进入挑战流程（payment_status={hint_payment_status}, reason={hint_reason}）。"
                        "请打开 checkout 页面完成 challenge 后点击“我已完成支付”或“同步订阅”。"
                    )
                    db.commit()
                    db.refresh(task)
                    return {
                        "success": True,
                        "verified": False,
                        "pending": True,
                        "need_user_action": True,
                        "subscription_type": str(account.subscription_type or "free"),
                        "task": serialize_task_fn(task),
                        "account_id": account.id,
                        "account_email": account.email,
                        "third_party": {
                            "submitted": True,
                            "api_url": used_endpoint,
                            "assessment": third_party_assessment,
                            "poll": {},
                            "response": third_party_response_safe,
                        },
                    }

                third_party_status_poll = poll_status_fn(
                    api_url=used_endpoint,
                    api_key=api_key,
                    checkout_session_id=checkout_session_id,
                    proxy=proxy,
                    timeout_seconds=request.third_party_poll_timeout_seconds,
                    interval_seconds=request.third_party_poll_interval_seconds,
                    status_hints=assess_snapshot or {},
                )
                poll_state = str((third_party_status_poll or {}).get("state") or "pending").lower()
                poll_reason = str((third_party_status_poll or {}).get("reason") or "").strip()
                poll_snapshot = (third_party_status_poll or {}).get("snapshot") or {}
                poll_payment_status = str((poll_snapshot or {}).get("payment_status") or "").lower()

                if poll_state == "failed":
                    task.status = "failed"
                    task.last_error = f"第三方状态失败: {poll_reason or 'unknown'}"
                    task.last_checked_at = datetime.utcnow()
                    db.commit()
                    raise ValueError(f"第三方状态失败: {poll_reason or 'unknown'}")

                if poll_state != "success":
                    task.status = "waiting_user_action"
                    task.last_checked_at = datetime.utcnow()
                    hint_reason = poll_reason or assess_reason or "pending_confirmation"
                    hint_payment_status = poll_payment_status or payment_status or "unknown"
                    task.last_error = (
                        f"第三方已受理，等待支付最终状态（payment_status={hint_payment_status}, reason={hint_reason}）。"
                        "如页面要求 challenge，请完成后点击“我已完成支付”或“同步订阅”。"
                    )
                    db.commit()
                    db.refresh(task)
                    return {
                        "success": True,
                        "verified": False,
                        "pending": True,
                        "need_user_action": True,
                        "subscription_type": str(account.subscription_type or "free"),
                        "task": serialize_task_fn(task),
                        "account_id": account.id,
                        "account_email": account.email,
                        "third_party": {
                            "submitted": True,
                            "api_url": used_endpoint,
                            "assessment": third_party_assessment,
                            "poll": third_party_status_poll,
                            "response": third_party_response_safe,
                        },
                    }

                paid_hint_status = poll_payment_status or payment_status or "paid"
                paid_hint_reason = poll_reason or assess_reason or "third_party_paid_signal"

            mark_paid_pending_fn(
                task,
                (
                    f"支付已确认（payment_status={paid_hint_status}, reason={paid_hint_reason}）。"
                    "等待订阅状态同步，可点击“同步订阅”刷新。"
                ),
            )
            db.commit()
            db.refresh(task)
            return {
                "success": True,
                "verified": False,
                "paid_confirmed": True,
                "subscription_type": str(account.subscription_type or "free"),
                "task": serialize_task_fn(task),
                "account_id": account.id,
                "account_email": account.email,
                "third_party": {
                    "submitted": True,
                    "api_url": used_endpoint,
                    "assessment": third_party_assessment,
                    "poll": third_party_status_poll,
                    "response": third_party_response_safe,
                },
            }

    def auto_bind_local(self, task_id: int, request, **kwargs):
        serialize_task_fn = kwargs["serialize_task_fn"]
        resolve_proxy_fn = kwargs["resolve_proxy_fn"]
        extract_checkout_session_id_fn = kwargs["extract_checkout_session_id_fn"]
        build_checkout_url_fn = kwargs["build_checkout_url_fn"]
        resolve_device_id_fn = kwargs["resolve_device_id_fn"]
        extract_session_token_fn = kwargs["extract_session_token_fn"]
        bootstrap_session_token_fn = kwargs["bootstrap_session_token_fn"]
        auto_bind_checkout_fn = kwargs["auto_bind_checkout_fn"]
        mark_paid_pending_fn = kwargs["mark_paid_pending_fn"]
        open_url_fn = kwargs["open_url_fn"]

        with self.manager.session_scope() as db:
            task = db.query(BindCardTask).options(joinedload(BindCardTask.account)).filter(BindCardTask.id == task_id).first()
            if not task:
                raise LookupError("绑卡任务不存在")
            account = task.account
            if not account:
                raise LookupError("任务关联账号不存在")

            checkout_session_id = str(task.checkout_session_id or "").strip() or extract_checkout_session_id_fn(task.checkout_url)
            checkout_url = build_checkout_url_fn(checkout_session_id) or str(task.checkout_url or "").strip()
            if not checkout_url:
                raise ValueError("任务缺少 checkout 链接，请重新创建任务")

            card_number = str(request.card.number or "").strip()
            card_cvc = str(request.card.cvc or "").strip()
            if not card_number or not card_cvc:
                raise ValueError("卡号/CVC 不能为空")

            task.bind_mode = "local_auto"
            task.status = "verifying"
            task.last_error = None
            task.last_checked_at = datetime.utcnow()
            db.commit()

            runtime_proxy = resolve_proxy_fn(request.proxy, account)
            resolved_device_id = resolve_device_id_fn(account)
            resolved_session_token = str(account.session_token or "").strip() or extract_session_token_fn(account.cookies)
            if not resolved_session_token and not runtime_proxy:
                task.status = "failed"
                task.last_checked_at = datetime.utcnow()
                task.last_error = "当前账号缺少 session_token，且未检测到可用代理。请在设置中配置代理（或为本次任务传入 proxy）后重试全自动。"
                db.commit()
                raise ValueError(task.last_error)

            if not resolved_session_token:
                resolved_session_token = bootstrap_session_token_fn(
                    db=db,
                    account=account,
                    proxy=runtime_proxy,
                )
            if not resolved_session_token:
                task.status = "failed"
                task.last_checked_at = datetime.utcnow()
                task.last_error = (
                    "会话补全未拿到 session_token。"
                    "请先在支付页执行“会话诊断/自动补会话”，"
                    "或手动粘贴 session_token 后再重试全自动绑卡。"
                )
                db.commit()
                raise ValueError(task.last_error)

            try:
                browser_result = auto_bind_checkout_fn(
                    checkout_url=checkout_url,
                    cookies_str=str(account.cookies or ""),
                    session_token=resolved_session_token,
                    access_token=str(account.access_token or ""),
                    device_id=resolved_device_id,
                    card_number=card_number,
                    exp_month=str(request.card.exp_month or ""),
                    exp_year=str(request.card.exp_year or ""),
                    cvc=card_cvc,
                    billing_name=str(request.profile.name or "").strip(),
                    billing_country=str(request.profile.country or "US").strip().upper(),
                    billing_line1=str(request.profile.line1 or "").strip(),
                    billing_city=str(request.profile.city or "").strip(),
                    billing_state=str(request.profile.state or "").strip(),
                    billing_postal=str(request.profile.postal or "").strip(),
                    proxy=runtime_proxy,
                    timeout_seconds=request.browser_timeout_seconds,
                    post_submit_wait_seconds=request.post_submit_wait_seconds,
                    headless=bool(request.headless),
                )
            except Exception as exc:
                task.status = "failed"
                task.last_error = f"本地自动绑卡执行异常: {exc}"
                task.last_checked_at = datetime.utcnow()
                db.commit()
                raise RuntimeError(f"本地自动绑卡执行失败: {exc}")

            success = bool(browser_result.get("success"))
            need_user_action = bool(browser_result.get("need_user_action"))
            pending = bool(browser_result.get("pending"))
            stage = str(browser_result.get("stage") or "").strip()
            message = str(browser_result.get("error") or browser_result.get("message") or "").strip()

            if not success:
                if "playwright not installed" in message.lower():
                    task.status = "failed"
                    task.last_checked_at = datetime.utcnow()
                    task.last_error = (
                        "本地自动绑卡环境缺少 Playwright/Chromium。"
                        "请先执行: pip install playwright && playwright install chromium"
                    )
                    db.commit()
                    raise ValueError(task.last_error)

                if need_user_action or pending:
                    if stage in ("cdp_session_missing", "navigate_checkout"):
                        hint = (
                            "自动会话未建立（checkout 重定向到首页）。"
                            "请先在“支付页-半自动”打开一次官方 checkout 完成登录态预热，"
                            "随后再次执行“全自动”。"
                        )
                    else:
                        hint = (
                            f"本地自动绑卡已执行到 {stage or 'unknown'}，当前需要人工继续完成（{message or 'challenge_or_pending'}）。"
                            "请在支付页完成后点击“我已完成支付”或“同步订阅”。"
                        )
                    manual_opened = False
                    if stage in ("cdp_challenge", "challenge"):
                        try:
                            manual_opened = open_url_fn(checkout_url, str(account.cookies or ""))
                        except Exception:
                            manual_opened = False
                        if manual_opened:
                            hint += " 已自动为你打开手动验证窗口。"
                    task.status = "waiting_user_action"
                    task.last_checked_at = datetime.utcnow()
                    task.last_error = hint
                    db.commit()
                    db.refresh(task)
                    return {
                        "success": True,
                        "verified": False,
                        "pending": True,
                        "need_user_action": True,
                        "subscription_type": str(account.subscription_type or "free"),
                        "task": serialize_task_fn(task),
                        "account_id": account.id,
                        "account_email": account.email,
                        "manual_opened": manual_opened,
                        "local_auto": browser_result,
                    }

                task.status = "failed"
                task.last_checked_at = datetime.utcnow()
                task.last_error = f"本地自动绑卡失败: {message or 'unknown_error'}"
                db.commit()
                raise ValueError(task.last_error)

            mark_paid_pending_fn(
                task,
                (
                    f"本地自动绑卡已完成支付提交（stage={stage or 'complete'}）。"
                    "等待订阅状态同步，可点击“同步订阅”刷新。"
                ),
            )
            db.commit()
            db.refresh(task)
            return {
                "success": True,
                "verified": False,
                "paid_confirmed": True,
                "subscription_type": str(account.subscription_type or "free"),
                "task": serialize_task_fn(task),
                "account_id": account.id,
                "account_email": account.email,
                "local_auto": browser_result,
            }
