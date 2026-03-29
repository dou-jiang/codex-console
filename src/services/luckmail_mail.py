"""
LuckMail 邮箱服务实现
"""

import logging
import json
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .base import BaseEmailService, EmailServiceError, EmailServiceType
from ..config.constants import OTP_CODE_PATTERN


logger = logging.getLogger(__name__)
_STATE_LOCK = threading.RLock()
# 申诉硬编码开关（临时）：False=关闭申诉提交；True=开启申诉提交。
LUCKMAIL_APPEAL_ENABLED = False


def _load_luckmail_client_class():
    """
    兼容两种来源：
    1) 环境已安装 luckmail 包
    2) 本地 vendored 目录（优先 codex-console/luckmail，其次 ../tools/luckmail）
    """
    try:
        from luckmail import LuckMailClient  # type: ignore

        return LuckMailClient
    except Exception:
        pass

    candidates = [
        Path(__file__).resolve().parents[2] / "luckmail",
        Path(__file__).resolve().parents[3] / "tools" / "luckmail",
    ]
    for path in candidates:
        if not path.is_dir():
            continue
        path_str = str(path)
        if path_str not in sys.path:
            sys.path.insert(0, path_str)
        try:
            from luckmail import LuckMailClient  # type: ignore

            return LuckMailClient
        except Exception:
            continue
    return None


class LuckMailService(BaseEmailService):
    """LuckMail 接码邮箱服务"""

    def __init__(self, config: Dict[str, Any] = None, name: str = None):
        super().__init__(EmailServiceType.LUCKMAIL, name)

        default_config = {
            "base_url": "https://mails.luckyous.com/",
            "api_key": "",
            "project_code": "openai",
            "email_type": "ms_graph",
            "preferred_domain": "",
            # purchase: 购买邮箱 + token 拉码（可多次）
            # order: 创建接码订单 + 订单拉码（通常一次）
            "inbox_mode": "purchase",
            # 任务开始时优先复用“未在账号库且不在本地黑名单”的已购邮箱
            "reuse_existing_purchases": True,
            "purchase_scan_pages": 5,
            "purchase_scan_page_size": 100,
            "timeout": 30,
            "max_retries": 3,
            "poll_interval": 3.0,
            "code_reuse_ttl": 600,
        }
        self.config = {**default_config, **(config or {})}

        self.config["base_url"] = str(self.config.get("base_url") or "").strip()
        if not self.config["base_url"]:
            raise ValueError("LuckMail 配置缺少 base_url")
        self.config["api_key"] = str(self.config.get("api_key") or "").strip()
        self.config["project_code"] = str(self.config.get("project_code") or "openai").strip()
        self.config["email_type"] = str(self.config.get("email_type") or "ms_graph").strip()
        self.config["preferred_domain"] = str(self.config.get("preferred_domain") or "").strip().lstrip("@")
        self.config["inbox_mode"] = self._normalize_inbox_mode(self.config.get("inbox_mode"))
        self.config["reuse_existing_purchases"] = bool(self.config.get("reuse_existing_purchases", True))
        self.config["purchase_scan_pages"] = max(int(self.config.get("purchase_scan_pages") or 5), 1)
        self.config["purchase_scan_page_size"] = max(int(self.config.get("purchase_scan_page_size") or 100), 1)
        self.config["poll_interval"] = float(self.config.get("poll_interval") or 3.0)
        self.config["code_reuse_ttl"] = int(self.config.get("code_reuse_ttl") or 600)

        if not self.config["api_key"]:
            raise ValueError("LuckMail 配置缺少 api_key")
        if not self.config["project_code"]:
            raise ValueError("LuckMail 配置缺少 project_code")

        client_cls = _load_luckmail_client_class()
        if client_cls is None:
            raise ValueError(
                "未找到 LuckMail SDK，请先安装 luckmail 包或确保本地存在 tools/luckmail"
            )

        try:
            self.client = client_cls(
                base_url=self.config["base_url"],
                api_key=self.config["api_key"],
            )
        except Exception as exc:
            raise ValueError(f"初始化 LuckMail 客户端失败: {exc}")

        self._orders_by_no: Dict[str, Dict[str, Any]] = {}
        self._orders_by_email: Dict[str, Dict[str, Any]] = {}
        # 记录每个订单/Token 最近返回过的验证码，避免后续阶段反复拿到旧码。
        self._recent_codes_by_order: Dict[str, Dict[str, float]] = {}
        self._data_dir = Path(__file__).resolve().parents[2] / "data"
        self._registered_file = self._data_dir / "luckmail_registered_emails.json"
        self._failed_file = self._data_dir / "luckmail_failed_emails.json"

    def _normalize_inbox_mode(self, raw: Any) -> str:
        mode = str(raw or "").strip().lower()
        aliases = {
            "purchase": "purchase",
            "token": "purchase",
            "buy": "purchase",
            "purchased": "purchase",
            "order": "order",
            "code": "order",
        }
        return aliases.get(mode, "purchase")

    def _extract_field(self, obj: Any, *keys: str) -> Any:
        if obj is None:
            return None
        if isinstance(obj, dict):
            for k in keys:
                if k in obj:
                    return obj.get(k)
            return None
        for k in keys:
            if hasattr(obj, k):
                return getattr(obj, k)
        return None

    def _cache_order(self, info: Dict[str, Any]) -> None:
        order_key = str(info.get("order_no") or info.get("service_id") or "").strip()
        email = str(info.get("email") or "").strip().lower()
        if order_key:
            self._orders_by_no[order_key] = info
        if email:
            self._orders_by_email[email] = info

    def _find_order(self, email: Optional[str], email_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if email_id:
            item = self._orders_by_no.get(str(email_id).strip())
            if item:
                return item
        if email:
            item = self._orders_by_email.get(str(email).strip().lower())
            if item:
                return item
        return None

    def _is_recent_code(self, order_key: str, code: str, now: Optional[float] = None) -> bool:
        if not order_key or not code:
            return False
        now_ts = now or time.time()
        ttl = max(int(self.config.get("code_reuse_ttl") or 600), 0)
        order_cache = self._recent_codes_by_order.get(order_key) or {}
        if ttl <= 0:
            return code in order_cache
        used_at = order_cache.get(code)
        if used_at is None:
            return False
        return (now_ts - used_at) <= ttl

    def _remember_code(self, order_key: str, code: str, now: Optional[float] = None) -> None:
        if not order_key or not code:
            return
        now_ts = now or time.time()
        ttl = max(int(self.config.get("code_reuse_ttl") or 600), 0)
        order_cache = self._recent_codes_by_order.setdefault(order_key, {})
        order_cache[code] = now_ts
        if ttl > 0:
            expire_before = now_ts - ttl
            stale = [k for k, v in order_cache.items() if v < expire_before]
            for key in stale:
                order_cache.pop(key, None)

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _normalize_email(self, email: Optional[str]) -> str:
        return str(email or "").strip().lower()

    def _is_resumable_failure_reason(self, reason: str) -> bool:
        text = str(reason or "").strip().lower()
        if not text:
            return False
        keywords = (
            "该邮箱已存在 openai",
            "邮箱已存在 openai",
            "user_already_exists",
            "already exists",
            "创建用户账户失败",
        )
        return any(k in text for k in keywords)

    def _extract_password_from_task_logs(self, logs_text: str) -> str:
        if not logs_text:
            return ""
        matches = re.findall(r"生成密码[:：]\s*([^\s]+)", str(logs_text))
        if not matches:
            return ""
        return str(matches[-1] or "").strip()

    def _recover_password_from_recent_task_logs(self, email: str, max_tasks: int = 30) -> str:
        email_norm = self._normalize_email(email)
        if not email_norm:
            return ""
        try:
            from sqlalchemy import desc
            from ..database.models import RegistrationTask as RegistrationTaskModel
            from ..database.session import get_db

            with get_db() as db:
                tasks = (
                    db.query(RegistrationTaskModel)
                    .filter(RegistrationTaskModel.logs.isnot(None))
                    .order_by(desc(RegistrationTaskModel.created_at))
                    .limit(max_tasks)
                    .all()
                )

            for task in tasks:
                logs_text = str(getattr(task, "logs", "") or "")
                if email_norm not in logs_text.lower():
                    continue
                recovered = self._extract_password_from_task_logs(logs_text)
                if recovered:
                    return recovered
        except Exception as exc:
            logger.warning(f"LuckMail 从任务日志恢复密码失败: {exc}")
        return ""

    def _load_email_index(self, path: Path) -> Dict[str, Dict[str, Any]]:
        with _STATE_LOCK:
            try:
                if not path.exists():
                    return {}
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    return {
                        self._normalize_email(e): {"email": self._normalize_email(e), "updated_at": self._now_iso()}
                        for e in raw
                        if self._normalize_email(e)
                    }
                if not isinstance(raw, dict):
                    return {}
                payload = raw.get("emails", raw)
                if isinstance(payload, list):
                    return {
                        self._normalize_email(e): {"email": self._normalize_email(e), "updated_at": self._now_iso()}
                        for e in payload
                        if self._normalize_email(e)
                    }
                if isinstance(payload, dict):
                    result: Dict[str, Dict[str, Any]] = {}
                    for email_key, meta in payload.items():
                        email_norm = self._normalize_email(email_key)
                        if not email_norm:
                            continue
                        if isinstance(meta, dict):
                            record = meta.copy()
                        else:
                            record = {"value": meta}
                        record["email"] = email_norm
                        if "updated_at" not in record:
                            record["updated_at"] = self._now_iso()
                        result[email_norm] = record
                    return result
            except Exception as exc:
                logger.warning(f"LuckMail 读取状态文件失败: {path} - {exc}")
                return {}
        return {}

    def _save_email_index(self, path: Path, index: Dict[str, Dict[str, Any]]) -> None:
        with _STATE_LOCK:
            try:
                self._data_dir.mkdir(parents=True, exist_ok=True)
                payload = {
                    "updated_at": self._now_iso(),
                    "count": len(index),
                    "emails": index,
                }
                tmp_path = path.with_suffix(path.suffix + ".tmp")
                tmp_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                tmp_path.replace(path)
            except Exception as exc:
                logger.warning(f"LuckMail 写入状态文件失败: {path} - {exc}")

    def _mark_registered_email(self, email: str, extra: Optional[Dict[str, Any]] = None) -> None:
        email_norm = self._normalize_email(email)
        if not email_norm:
            return
        registered = self._load_email_index(self._registered_file)
        failed = self._load_email_index(self._failed_file)
        record = registered.get(email_norm, {"email": email_norm})
        record["updated_at"] = self._now_iso()
        if extra:
            for k, v in extra.items():
                if v is not None and v != "":
                    record[k] = v
        registered[email_norm] = record
        failed.pop(email_norm, None)
        self._save_email_index(self._registered_file, registered)
        self._save_email_index(self._failed_file, failed)

    def _should_force_failed_record(self, reason: str) -> bool:
        text = str(reason or "").strip().lower()
        if not text:
            return False
        keywords = (
            "该邮箱已存在 openai",
            "邮箱已存在 openai",
            "user_already_exists",
            "already exists",
            "failed to register username",
            "用户名注册失败",
            "创建用户账户失败",
        )
        return any(k in text for k in keywords)

    def _reconcile_failed_over_registered(
        self,
        registered: Dict[str, Dict[str, Any]],
        failed: Dict[str, Dict[str, Any]],
    ) -> bool:
        changed = False
        for email, failed_meta in list(failed.items()):
            if email not in registered:
                continue
            failed_reason = str((failed_meta or {}).get("reason") or "")
            if self._should_force_failed_record(failed_reason):
                registered.pop(email, None)
                changed = True
        return changed

    def _mark_failed_email(
        self,
        email: str,
        reason: str = "",
        extra: Optional[Dict[str, Any]] = None,
        prefer_failed: bool = False,
    ) -> Dict[str, Any]:
        email_norm = self._normalize_email(email)
        if not email_norm:
            return {}

        registered = self._load_email_index(self._registered_file)
        registered_record: Dict[str, Any] = {}
        if email_norm in registered:
            if not prefer_failed:
                return registered.get(email_norm) or {}
            registered_record = dict(registered.get(email_norm) or {})
            registered.pop(email_norm, None)
            self._save_email_index(self._registered_file, registered)

        failed = self._load_email_index(self._failed_file)
        record = failed.get(email_norm, {"email": email_norm, "fail_count": 0})
        for k, v in registered_record.items():
            if k not in record and v not in (None, ""):
                record[k] = v
        record["fail_count"] = int(record.get("fail_count") or 0) + 1
        record["updated_at"] = self._now_iso()
        if reason:
            record["reason"] = reason[:500]
        if extra:
            for k, v in extra.items():
                if v is not None and v != "":
                    record[k] = v
        failed[email_norm] = record
        self._save_email_index(self._failed_file, failed)
        return record

    def mark_registration_outcome(
        self,
        email: str,
        success: bool,
        reason: str = "",
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """供任务调度层调用：把注册结果落盘，避免后续重复尝试同邮箱。"""
        if success:
            self._mark_registered_email(email, extra=context)
        else:
            prefer_failed = self._should_force_failed_record(reason)
            context_copy = dict(context or {})
            password = str(context_copy.get("generated_password") or context_copy.get("password") or "").strip()
            if password:
                context_copy["password"] = password
            record = self._mark_failed_email(
                email,
                reason=reason,
                extra=context_copy,
                prefer_failed=prefer_failed,
            )
            self._try_submit_appeal(email=email, reason=reason, context=context_copy, failed_record=record)

    def _resolve_order_id_by_order_no(self, order_no: str, max_pages: int = 3, page_size: int = 50) -> Optional[int]:
        order_no_text = str(order_no or "").strip()
        if not order_no_text:
            return None
        try:
            for page in range(1, max_pages + 1):
                result = self.client.user.get_orders(page=page, page_size=page_size)
                items = list(getattr(result, "list", []) or [])
                if not items:
                    break
                for item in items:
                    current_order_no = str(self._extract_field(item, "order_no") or "").strip()
                    if current_order_no != order_no_text:
                        continue
                    order_id_raw = self._extract_field(item, "id", "order_id")
                    if order_id_raw in (None, ""):
                        continue
                    try:
                        return int(order_id_raw)
                    except Exception:
                        continue
                if len(items) < page_size:
                    break
        except Exception as exc:
            logger.warning(f"LuckMail 查询订单ID失败: {exc}")
        return None

    def _build_appeal_payload(
        self,
        reason: str,
        context: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        reason_text = str(reason or "").strip()
        reason_lower = reason_text.lower()

        purchase_id_raw = context.get("purchase_id")
        order_id_raw = context.get("order_id")
        order_no = str(context.get("order_no") or "").strip()

        appeal_type = None
        order_id = None
        purchase_id = None

        if purchase_id_raw not in (None, ""):
            try:
                purchase_id = int(purchase_id_raw)
                appeal_type = 2
            except Exception:
                purchase_id = None

        if appeal_type is None and order_id_raw not in (None, ""):
            try:
                order_id = int(order_id_raw)
                appeal_type = 1
            except Exception:
                order_id = None

        if appeal_type is None and order_no:
            order_id = self._resolve_order_id_by_order_no(order_no)
            if order_id is not None:
                appeal_type = 1

        if appeal_type is None:
            return None

        if "429" in reason_lower or "limit" in reason_lower or "限流" in reason_text:
            appeal_reason = "no_code"
        elif "exists" in reason_lower or "already" in reason_lower or "已存在" in reason_text:
            appeal_reason = "email_invalid"
        elif "验证码" in reason_text or "otp" in reason_lower:
            appeal_reason = "wrong_code"
        else:
            appeal_reason = "no_code"

        desc = reason_text or "注册任务失败，申请人工核查并处理。"
        payload: Dict[str, Any] = {
            "appeal_type": appeal_type,
            "reason": appeal_reason,
            "description": desc[:300],
        }
        if appeal_type == 1 and order_id is not None:
            payload["order_id"] = int(order_id)
        if appeal_type == 2 and purchase_id is not None:
            payload["purchase_id"] = int(purchase_id)
        return payload

    def _try_submit_appeal(
        self,
        email: str,
        reason: str,
        context: Dict[str, Any],
        failed_record: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not LUCKMAIL_APPEAL_ENABLED:
            return

        reason_text = str(reason or "").strip()
        if not reason_text:
            return

        reason_lower = reason_text.lower()
        should_appeal = (
            "429" in reason_lower
            or "限流" in reason_text
            or "验证码" in reason_text
            or "otp" in reason_lower
            or "failed to register username" in reason_lower
            or "用户名注册失败" in reason_text
            or "创建用户账户失败" in reason_text
            or "该邮箱已存在 openai" in reason_lower
            or "user_already_exists" in reason_lower
            or "already exists" in reason_lower
        )
        if not should_appeal:
            return

        email_norm = self._normalize_email(email)
        if not email_norm:
            return

        failed_index = self._load_email_index(self._failed_file)
        current = failed_index.get(email_norm, {})
        if not current and failed_record:
            current = failed_record

        # 申诉不代表删除：仅记录状态，不从 failed 名单移除。
        last_appeal_status = str(current.get("appeal_status") or "").strip().lower()
        if last_appeal_status == "submitted":
            return

        payload = self._build_appeal_payload(reason_text, context)
        if not payload:
            return

        try:
            response = self.client.user.create_appeal(**payload)
            appeal_no = str(self._extract_field(response, "appeal_no") or "").strip()
            current["appeal_status"] = "submitted"
            current["appeal_at"] = self._now_iso()
            if appeal_no:
                current["appeal_no"] = appeal_no
            failed_index[email_norm] = current
            self._save_email_index(self._failed_file, failed_index)
            logger.info(f"LuckMail 已提交申诉: email={email_norm}, appeal_no={appeal_no or '-'}")
        except Exception as exc:
            current["appeal_status"] = "failed"
            current["appeal_error"] = str(exc)[:500]
            current["appeal_at"] = self._now_iso()
            failed_index[email_norm] = current
            self._save_email_index(self._failed_file, failed_index)
            logger.warning(f"LuckMail 提交申诉失败: email={email_norm}, error={exc}")

    def _query_existing_account_emails(self, emails: Set[str]) -> Set[str]:
        if not emails:
            return set()
        try:
            from sqlalchemy import func
            from ..database.models import Account as AccountModel
            from ..database.session import get_db

            normalized = [self._normalize_email(e) for e in emails if self._normalize_email(e)]
            if not normalized:
                return set()

            with get_db() as db:
                rows = (
                    db.query(func.lower(AccountModel.email))
                    .filter(func.lower(AccountModel.email).in_(normalized))
                    .all()
                )
            result = set()
            for row in rows:
                try:
                    value = row[0]
                except Exception:
                    value = ""
                email_norm = self._normalize_email(value)
                if email_norm:
                    result.add(email_norm)
            return result
        except Exception as exc:
            logger.warning(f"LuckMail 查询账号库邮箱失败: {exc}")
            return set()

    def _iter_purchase_items(self, scan_pages: int, page_size: int):
        for page in range(1, scan_pages + 1):
            try:
                page_result = self.client.user.get_purchases(
                    page=page,
                    page_size=page_size,
                    user_disabled=0,
                )
            except Exception as exc:
                logger.warning(f"LuckMail 拉取已购邮箱失败: page={page}, error={exc}")
                break

            items = list(getattr(page_result, "list", []) or [])
            if not items:
                break

            for item in items:
                yield item

            if len(items) < page_size:
                break

    def _build_purchase_order_info(
        self,
        item: Any,
        project_code: str,
        email_type: str,
        preferred_domain: str,
        source: str,
    ) -> Optional[Dict[str, Any]]:
        email = self._normalize_email(self._extract_field(item, "email_address", "address", "email"))
        token = str(self._extract_field(item, "token") or "").strip()
        purchase_id_raw = self._extract_field(item, "id", "purchase_id")
        purchase_id = str(purchase_id_raw).strip() if purchase_id_raw not in (None, "") else ""

        if not email or not token:
            return None

        if preferred_domain:
            domain = email.split("@", 1)[1] if "@" in email else ""
            if domain != preferred_domain.lower():
                return None

        return {
            "id": purchase_id or token,
            "service_id": token,
            "order_no": "",
            "email": email,
            "token": token,
            "purchase_id": purchase_id or None,
            "inbox_mode": "purchase",
            "project_code": project_code,
            "email_type": email_type,
            "preferred_domain": preferred_domain,
            "expired_at": "",
            "created_at": time.time(),
            "source": source,
        }

    def _pick_reusable_purchase_inbox(
        self,
        project_code: str,
        email_type: str,
        preferred_domain: str,
    ) -> Optional[Dict[str, Any]]:
        registered = self._load_email_index(self._registered_file)
        failed = self._load_email_index(self._failed_file)
        if self._reconcile_failed_over_registered(registered, failed):
            self._save_email_index(self._registered_file, registered)

        candidates: List[Dict[str, Any]] = []
        for item in self._iter_purchase_items(
            scan_pages=int(self.config.get("purchase_scan_pages") or 5),
            page_size=int(self.config.get("purchase_scan_page_size") or 100),
        ):
            info = self._build_purchase_order_info(
                item=item,
                project_code=project_code,
                email_type=email_type,
                preferred_domain=preferred_domain,
                source="reuse_purchase",
            )
            if not info:
                continue
            email = self._normalize_email(info.get("email"))
            if not email:
                continue
            if email in registered:
                continue
            if email in failed:
                failed_meta = failed.get(email) or {}
                failed_reason = str(failed_meta.get("reason") or "")
                if not self._is_resumable_failure_reason(failed_reason):
                    continue

                resume_password = str(
                    failed_meta.get("password")
                    or failed_meta.get("generated_password")
                    or ""
                ).strip()
                if not resume_password:
                    resume_password = self._recover_password_from_recent_task_logs(email)
                if not resume_password:
                    continue

                info["resume_password"] = resume_password
                info["source"] = "resume_failed"
            candidates.append(info)

        if not candidates:
            return None

        existing_in_db = self._query_existing_account_emails({self._normalize_email(c.get("email")) for c in candidates})
        for info in candidates:
            email = self._normalize_email(info.get("email"))
            if email in existing_in_db:
                self._mark_registered_email(
                    email,
                    extra={
                        "source": "accounts_db",
                        "token": info.get("token"),
                        "purchase_id": info.get("purchase_id"),
                    },
                )
                continue
            return info
        return None

    def _create_order_inbox(
        self,
        project_code: str,
        email_type: str,
        preferred_domain: str,
        specified_email: Optional[str] = None,
    ) -> Dict[str, Any]:
        try:
            kwargs: Dict[str, Any] = {
                "project_code": project_code,
                "email_type": email_type,
            }
            if preferred_domain:
                kwargs["domain"] = preferred_domain
            if specified_email:
                kwargs["specified_email"] = specified_email
            order = self.client.user.create_order(**kwargs)
        except Exception as exc:
            self.update_status(False, exc)
            raise EmailServiceError(f"LuckMail 创建订单失败: {exc}")

        order_no = str(self._extract_field(order, "order_no") or "").strip()
        email = str(self._extract_field(order, "email_address", "email") or "").strip().lower()
        if not order_no or not email:
            raise EmailServiceError("LuckMail 返回订单信息不完整")

        return {
            "id": order_no,
            "service_id": order_no,
            "order_no": order_no,
            "email": email,
            "token": "",
            "purchase_id": None,
            "inbox_mode": "order",
            "project_code": project_code,
            "email_type": email_type,
            "preferred_domain": preferred_domain,
            "expired_at": str(self._extract_field(order, "expired_at") or "").strip(),
            "created_at": time.time(),
            "source": "new_order",
        }

    def _extract_first_purchase_item(self, purchased: Any) -> Any:
        if purchased is None:
            return None

        if isinstance(purchased, list):
            return purchased[0] if purchased else None

        if isinstance(purchased, dict):
            for key in ("purchases", "list", "items"):
                arr = purchased.get(key)
                if isinstance(arr, list) and arr:
                    return arr[0]
            data = purchased.get("data")
            if isinstance(data, dict):
                for key in ("purchases", "list", "items"):
                    arr = data.get(key)
                    if isinstance(arr, list) and arr:
                        return arr[0]
            return None

        for key in ("purchases", "list", "items"):
            arr = getattr(purchased, key, None)
            if isinstance(arr, list) and arr:
                return arr[0]

        return None

    def _create_purchase_inbox(
        self,
        project_code: str,
        email_type: str,
        preferred_domain: str,
    ) -> Dict[str, Any]:
        try:
            kwargs: Dict[str, Any] = {
                "project_code": project_code,
                "quantity": 1,
                "email_type": email_type,
            }
            if preferred_domain:
                kwargs["domain"] = preferred_domain
            purchased = self.client.user.purchase_emails(**kwargs)
        except Exception as exc:
            self.update_status(False, exc)
            raise EmailServiceError(f"LuckMail 购买邮箱失败: {exc}")

        item = self._extract_first_purchase_item(purchased)
        if item is None:
            raise EmailServiceError("LuckMail 购买邮箱返回为空")

        email = str(self._extract_field(item, "email_address", "address", "email") or "").strip().lower()
        token = str(self._extract_field(item, "token") or "").strip()
        purchase_id_raw = self._extract_field(item, "id", "purchase_id")
        purchase_id = str(purchase_id_raw).strip() if purchase_id_raw not in (None, "") else None

        if not email or not token:
            raise EmailServiceError("LuckMail 购买邮箱返回字段不完整（缺少 email/token）")

        return {
            "id": purchase_id or token,
            "service_id": token,
            "order_no": "",
            "email": email,
            "token": token,
            "purchase_id": purchase_id,
            "inbox_mode": "purchase",
            "project_code": project_code,
            "email_type": email_type,
            "preferred_domain": preferred_domain,
            "expired_at": "",
            "created_at": time.time(),
            "source": "new_purchase",
        }

    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        request_config = config or {}
        project_code = str(request_config.get("project_code") or self.config["project_code"]).strip()
        email_type = str(request_config.get("email_type") or self.config["email_type"]).strip()
        preferred_domain = str(
            request_config.get("preferred_domain")
            or request_config.get("domain")
            or self.config.get("preferred_domain")
            or ""
        ).strip().lstrip("@")

        inbox_mode = self._normalize_inbox_mode(
            request_config.get("inbox_mode") or request_config.get("mode") or self.config.get("inbox_mode")
        )

        if inbox_mode == "order":
            order_info = self._create_order_inbox(
                project_code=project_code,
                email_type=email_type,
                preferred_domain=preferred_domain,
            )
        else:
            if bool(self.config.get("reuse_existing_purchases", True)):
                reused = self._pick_reusable_purchase_inbox(
                    project_code=project_code,
                    email_type=email_type,
                    preferred_domain=preferred_domain,
                )
                if reused:
                    order_info = reused
                else:
                    order_info = self._create_purchase_inbox(
                        project_code=project_code,
                        email_type=email_type,
                        preferred_domain=preferred_domain,
                    )
            else:
                order_info = self._create_purchase_inbox(
                    project_code=project_code,
                    email_type=email_type,
                    preferred_domain=preferred_domain,
                )

        self._cache_order(order_info)
        self.update_status(True)
        return order_info

    def get_verification_code(
        self,
        email: str,
        email_id: str = None,
        timeout: int = 120,
        pattern: str = OTP_CODE_PATTERN,
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        order_info = self._find_order(email=email, email_id=email_id)

        token = ""
        order_no = ""
        inbox_mode = self._normalize_inbox_mode(self.config.get("inbox_mode"))
        if order_info:
            token = str(order_info.get("token") or "").strip()
            order_no = str(order_info.get("order_no") or order_info.get("service_id") or "").strip()
            inbox_mode = self._normalize_inbox_mode(order_info.get("inbox_mode") or inbox_mode)

        if not token and email_id and str(email_id).strip().startswith("tok_"):
            token = str(email_id).strip()
            inbox_mode = "purchase"

        if not order_no and email_id and not token:
            order_no = str(email_id).strip()

        if inbox_mode == "purchase":
            if not token:
                logger.warning(f"LuckMail 未找到 token，无法拉取验证码: email={email}, email_id={email_id}")
                return None
            code_key = f"token:{token}"
        else:
            if not order_no:
                logger.warning(f"LuckMail 未找到订单号，无法拉取验证码: email={email}, email_id={email_id}")
                return None
            code_key = f"order:{order_no}"

        poll_interval = float(self.config.get("poll_interval") or 3.0)
        timeout_s = max(int(timeout or 120), 1)
        deadline = time.time() + timeout_s
        # OTP 刚发送后的短窗口内更容易读到旧码；配合“最近已用验证码”一起过滤。
        otp_guard_until = (float(otp_sent_at) + 1.5) if otp_sent_at else None

        while time.time() < deadline:
            try:
                if inbox_mode == "purchase":
                    result = self.client.user.get_token_code(token)
                    status = "success" if bool(self._extract_field(result, "has_new_mail")) else "pending"
                else:
                    result = self.client.user.get_order_code(order_no)
                    status = str(self._extract_field(result, "status") or "").strip().lower()
            except Exception as exc:
                logger.warning(f"LuckMail 拉取验证码失败: {exc}")
                self.update_status(False, exc)
                time.sleep(min(poll_interval, 1.0))
                continue

            code = str(self._extract_field(result, "verification_code") or "").strip()

            # token 模式下，部分平台会在 has_new_mail=false 时也返回最近一次 code。
            # 这里以 code 为准，再配合“最近已用验证码”过滤旧码。
            if inbox_mode == "purchase" and code:
                status = "success"

            if status in ("timeout", "cancelled"):
                ref = token if inbox_mode == "purchase" else order_no
                logger.info(f"LuckMail 未拿到验证码: {ref}, status={status}")
                return None

            if status == "success" and code:
                if pattern and not re.search(pattern, code):
                    logger.warning(f"LuckMail 返回验证码格式不匹配: {code}")
                    return None

                now_ts = time.time()
                if otp_guard_until and now_ts < otp_guard_until and self._is_recent_code(code_key, code, now_ts):
                    time.sleep(poll_interval)
                    continue

                if self._is_recent_code(code_key, code, now_ts):
                    # 同一 token/订单在不同流程阶段会复用查询接口，这里阻断旧码重复返回。
                    time.sleep(poll_interval)
                    continue

                self._remember_code(code_key, code, now_ts)
                self.update_status(True)
                return code

            time.sleep(poll_interval)

        return None

    def list_emails(self, **kwargs) -> List[Dict[str, Any]]:
        _ = kwargs
        return list(self._orders_by_no.values())

    def delete_email(self, email_id: str) -> bool:
        order_info = self._find_order(email=email_id, email_id=email_id)
        token = str((order_info or {}).get("token") or "").strip()
        purchase_id = str((order_info or {}).get("purchase_id") or "").strip()
        order_no = str((order_info or {}).get("order_no") or "").strip()

        if not token and not order_no:
            raw_id = str(email_id or "").strip()
            if raw_id.startswith("tok_"):
                token = raw_id
            else:
                order_no = raw_id

        if not token and not order_no:
            return False

        try:
            if token and purchase_id.isdigit():
                # 购买邮箱通常不支持直接删除，标记禁用即可。
                try:
                    self.client.user.set_purchase_disabled(int(purchase_id), 1)
                except Exception:
                    pass
            elif order_no:
                self.client.user.cancel_order(order_no)

            key = token or order_no
            item = self._orders_by_no.pop(key, None)
            if item:
                email = str(item.get("email") or "").strip().lower()
                if email:
                    self._orders_by_email.pop(email, None)
            if token:
                self._recent_codes_by_order.pop(f"token:{token}", None)
            if order_no:
                self._recent_codes_by_order.pop(f"order:{order_no}", None)
            self.update_status(True)
            return True
        except Exception as exc:
            logger.warning(f"LuckMail 删除邮箱失败: {exc}")
            self.update_status(False, exc)
            return False

    def check_health(self) -> bool:
        try:
            self.client.user.get_balance()
            self.update_status(True)
            return True
        except Exception as exc:
            logger.warning(f"LuckMail 健康检查失败: {exc}")
            self.update_status(False, exc)
            return False

    def get_service_info(self) -> Dict[str, Any]:
        return {
            "service_type": self.service_type.value,
            "name": self.name,
            "base_url": self.config.get("base_url"),
            "project_code": self.config.get("project_code"),
            "email_type": self.config.get("email_type"),
            "preferred_domain": self.config.get("preferred_domain"),
            "inbox_mode": self.config.get("inbox_mode"),
            "cached_orders": len(self._orders_by_no),
            "status": self.status.value,
        }
