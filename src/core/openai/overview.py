"""
Codex 账号总览数据抓取与解析
"""

from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from curl_cffi import requests as cffi_requests

from ...database.models import Account

logger = logging.getLogger(__name__)

_USAGE_ENDPOINTS: Tuple[Tuple[str, str, bool], ...] = (
    # required=True: 核心稳定端点，失败计入 errors
    ("me", "https://chatgpt.com/backend-api/me", True),
    ("wham_usage", "https://chatgpt.com/backend-api/wham/usage", True),
    # required=False: 非核心端点，仅做补充，失败时静默降级
    ("codex_usage", "https://chatgpt.com/backend-api/codex/usage", False),
)

_NUMERIC_KEYS_USED = ("used", "usage", "consumed", "spent", "count", "current", "value")
_NUMERIC_KEYS_TOTAL = ("total", "limit", "max", "quota", "allowance", "capacity")
_NUMERIC_KEYS_REMAINING = ("remaining", "left", "available", "remain")
_NUMERIC_KEYS_PERCENT = ("percent", "percentage", "ratio", "remaining_percent", "left_percent")
_RESET_AT_KEYS = (
    "reset_at",
    "resets_at",
    "next_reset",
    "next_reset_at",
    "reset_time",
    "renew_at",
    "expires_at",
    "expired_at",
)
_RESET_IN_KEYS = ("reset_in", "time_to_reset", "ttl_seconds", "remaining_seconds", "seconds_to_reset")
_HOURLY_WINDOW_MAX_SECONDS = 12 * 60 * 60
_WEEKLY_WINDOW_MIN_SECONDS = 5 * 24 * 60 * 60


def _build_proxies(proxy: Optional[str]) -> Optional[dict]:
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def _extract_cookie_value(cookies_str: str, key: str) -> Optional[str]:
    if not cookies_str:
        return None
    prefix = f"{key}="
    for part in cookies_str.split(";"):
        item = part.strip()
        if item.startswith(prefix):
            return item[len(prefix):].strip()
    return None


def _resolve_chatgpt_account_id(account: Account) -> Optional[str]:
    token_account_id = (
        _extract_chatgpt_account_id_from_jwt(account.access_token)
        or _extract_chatgpt_account_id_from_jwt(account.id_token)
    )
    for candidate in (token_account_id, account.account_id, account.workspace_id):
        value = str(candidate or "").strip()
        if value:
            return value
    return None


def _decode_jwt_payload(token: Optional[str]) -> Optional[Dict[str, Any]]:
    text = str(token or "").strip()
    if not text or "." not in text:
        return None
    parts = text.split(".")
    if len(parts) < 2:
        return None
    payload_part = parts[1]
    if not payload_part:
        return None
    padding = "=" * (-len(payload_part) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload_part + padding)
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _extract_auth_claim(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    auth = payload.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        return auth
    auth = payload.get("auth_data")
    if isinstance(auth, dict):
        return auth
    return {}


def _extract_chatgpt_account_id_from_jwt(token: Optional[str]) -> Optional[str]:
    payload = _decode_jwt_payload(token)
    if not payload:
        return None
    auth = _extract_auth_claim(payload)
    for key in ("chatgpt_account_id", "account_id", "workspace_id"):
        value = str(auth.get(key) or payload.get(key) or "").strip()
        if value:
            return value
    return None


def _extract_chatgpt_plan_from_jwt(token: Optional[str]) -> Optional[str]:
    payload = _decode_jwt_payload(token)
    if not payload:
        return None
    auth = _extract_auth_claim(payload)
    candidates = [
        auth.get("chatgpt_plan_type"),
        auth.get("plan_type"),
        auth.get("subscription_plan"),
        payload.get("chatgpt_plan_type"),
        payload.get("plan_type"),
        payload.get("subscription_plan"),
        payload.get("subscription_tier"),
    ]
    for item in candidates:
        if isinstance(item, str) and item.strip():
            return _normalize_plan(item)
    return None


def _build_headers(account: Account) -> Dict[str, str]:
    headers = {
        "Authorization": f"Bearer {account.access_token}",
        "Content-Type": "application/json",
    }
    account_id = _resolve_chatgpt_account_id(account)
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    if account.cookies:
        headers["cookie"] = account.cookies
        oai_did = _extract_cookie_value(account.cookies, "oai-did")
        if oai_did:
            headers["oai-device-id"] = oai_did
    return headers


def _request_json(url: str, headers: Dict[str, str], proxy: Optional[str]) -> Dict[str, Any]:
    resp = cffi_requests.get(
        url,
        headers=headers,
        proxies=_build_proxies(proxy),
        timeout=20,
        impersonate="chrome110",
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        return data
    return {"data": data}


def _extract_http_status(exc: Exception) -> Optional[int]:
    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return status
    match = re.search(r"HTTP Error\s+(\d{3})", str(exc))
    if match:
        try:
            return int(match.group(1))
        except Exception:
            return None
    return None


def _request_json_with_proxy_fallback(url: str, headers: Dict[str, str], proxy: Optional[str]) -> Dict[str, Any]:
    """
    配额抓取优先按当前代理请求；若代理异常则回退直连重试一次。
    """
    try:
        return _request_json(url, headers, proxy)
    except Exception as proxy_exc:
        if not proxy:
            raise
        status = _extract_http_status(proxy_exc)
        # 某些账号/端点天然 403/404，这里降噪避免刷屏 warning。
        if status in (401, 403, 404):
            logger.debug("概览请求代理回退直连: url=%s status=%s err=%s", url, status, proxy_exc)
        else:
            logger.warning("概览请求代理失败，回退直连重试: url=%s err=%s", url, proxy_exc)
        return _request_json(url, headers, None)


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None
    return None


def _pick_number(payload: Dict[str, Any], keys: Tuple[str, ...]) -> Optional[float]:
    lowered = {str(k).lower(): v for k, v in payload.items()}
    for key in keys:
        for actual_key, value in lowered.items():
            if key == actual_key or actual_key.endswith(f"_{key}") or actual_key.endswith(f".{key}"):
                number = _to_float(value)
                if number is not None:
                    return number
    return None


def _try_parse_epoch(value: float) -> Optional[datetime]:
    if value <= 0:
        return None
    # 同时兼容秒和毫秒
    if value > 10_000_000_000:
        value = value / 1000.0
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except Exception:
        return None


def _normalize_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    number = _to_float(value)
    if number is not None:
        return _try_parse_epoch(number)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def _format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    sec = max(int(seconds), 0)
    if sec < 60:
        return f"{sec}秒"
    minutes, remain_seconds = divmod(sec, 60)
    if minutes < 60:
        return f"{minutes}分{remain_seconds}秒"
    hours, remain_minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}小时{remain_minutes}分"
    days, remain_hours = divmod(hours, 24)
    return f"{days}天{remain_hours}小时"


def _detect_window_match(path: str, payload: Dict[str, Any], window: str) -> bool:
    path_lower = path.lower()
    if window == "hourly":
        keywords = ("hourly", "hour", "per_hour", "5h", "5_hour")
    else:
        keywords = ("weekly", "week", "per_week", "7d", "7_day")

    if any(token in path_lower for token in keywords):
        return True

    window_value = str(payload.get("window") or payload.get("period") or payload.get("scope") or "").lower()
    return any(token == window_value or token in window_value for token in keywords)


def _extract_quota_from_rate_limit_window(window_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(window_payload, dict):
        return None

    used_percent = _to_float(window_payload.get("used_percent"))
    remaining_percent = _to_float(window_payload.get("remaining_percent"))
    # 兼容比例值（0~1）与百分比值（0~100）
    if used_percent is not None and 0 <= used_percent <= 1:
        used_percent *= 100.0
    if remaining_percent is not None and 0 <= remaining_percent <= 1:
        remaining_percent *= 100.0
    if remaining_percent is None and used_percent is not None:
        remaining_percent = 100.0 - used_percent
    if remaining_percent is not None:
        remaining_percent = max(0.0, min(100.0, remaining_percent))

    total = (
        _to_float(window_payload.get("total"))
        or _to_float(window_payload.get("limit"))
        or _to_float(window_payload.get("max"))
        or _to_float(window_payload.get("capacity"))
    )
    used = _to_float(window_payload.get("used"))
    remaining = _to_float(window_payload.get("remaining"))
    limit_window_seconds = _to_float(
        window_payload.get("limit_window_seconds")
        or window_payload.get("window_seconds")
    )
    if limit_window_seconds is not None and limit_window_seconds <= 0:
        limit_window_seconds = None

    if total is not None and remaining is None and remaining_percent is not None:
        remaining = total * (remaining_percent / 100.0)
    if total is not None and used is None and remaining is not None:
        used = max(total - remaining, 0.0)
    if total is not None and remaining is None and used is not None:
        remaining = max(total - used, 0.0)

    reset_at = _normalize_datetime(
        window_payload.get("resets_at")
        or window_payload.get("reset_at")
        or window_payload.get("next_reset_at")
        or window_payload.get("next_reset")
    )
    reset_in_seconds = _to_float(
        window_payload.get("resets_in_seconds")
        or window_payload.get("remaining_seconds")
        or window_payload.get("seconds_to_reset")
        or window_payload.get("reset_in")
    )
    if reset_in_seconds is None and reset_at:
        reset_in_seconds = max((reset_at - datetime.now(timezone.utc)).total_seconds(), 0.0)
    if reset_at is None and reset_in_seconds is not None:
        reset_at = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=int(reset_in_seconds))

    if all(v is None for v in (total, used, remaining, remaining_percent, reset_at, reset_in_seconds)):
        return None

    if remaining_percent is None and total and total > 0 and remaining is not None:
        remaining_percent = max(0.0, min(100.0, (remaining / total) * 100.0))

    reset_in_text = _format_duration(reset_in_seconds)
    if reset_in_seconds is None and reset_at:
        reset_in_text = _format_duration((reset_at - datetime.now(timezone.utc)).total_seconds())

    return {
        "used": int(used) if used is not None else None,
        "total": int(total) if total is not None else None,
        "remaining": int(remaining) if remaining is not None else None,
        "percentage": round(float(remaining_percent), 2) if remaining_percent is not None else None,
        "reset_at": reset_at.isoformat() if reset_at else None,
        "reset_in_text": reset_in_text,
        "window_seconds": int(limit_window_seconds) if limit_window_seconds is not None else None,
        "status": "ok",
    }


def _infer_rate_limit_window_type(window_payload: Dict[str, Any], window_key: str) -> Tuple[str, bool]:
    """
    推断 rate_limit 窗口类型。
    返回: (window_type, confident)
    """
    seconds = _to_float(window_payload.get("limit_window_seconds") or window_payload.get("window_seconds"))
    if seconds is not None and seconds > 0:
        if seconds >= _WEEKLY_WINDOW_MIN_SECONDS:
            return "weekly", True
        if seconds <= _HOURLY_WINDOW_MAX_SECONDS:
            return "hourly", True
    return ("hourly", False) if window_key == "primary_window" else ("weekly", False)


def _select_rate_limit_window(rate_limit: Dict[str, Any], target_window: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    entries: List[Tuple[str, Dict[str, Any], str, bool]] = []
    for key in ("primary_window", "secondary_window"):
        raw = rate_limit.get(key)
        if not isinstance(raw, dict):
            continue
        inferred, confident = _infer_rate_limit_window_type(raw, key)
        entries.append((key, raw, inferred, confident))

    if not entries:
        return None

    # 1) 优先使用“基于窗口时长”的高置信匹配（避免把 7d 窗口误判成 5h）。
    for key, raw, inferred, confident in entries:
        if confident and inferred == target_window:
            return key, raw

    # 2) 再使用普通匹配（无时长信息时退化到 key 语义）。
    for key, raw, inferred, _ in entries:
        if inferred == target_window:
            return key, raw

    # 3) 保留历史 key 语义兜底（兼容单窗口账号场景，避免 5 小时或周配额出现 --）。
    fallback_key = "primary_window" if target_window == "hourly" else "secondary_window"
    for key, raw, _, _ in entries:
        if key == fallback_key:
            return key, raw
    return None


def _iter_rate_limit_candidates(payload: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    """
    从 payload 中提取可能的 rate_limit 容器。
    """
    if not isinstance(payload, dict):
        return []
    candidates: List[Tuple[str, Dict[str, Any]]] = []

    direct = payload.get("rate_limit")
    if isinstance(direct, dict):
        candidates.append(("rate_limit", direct))

    for parent_key in ("usage", "data", "quota", "limits", "codex"):
        parent = payload.get(parent_key)
        if not isinstance(parent, dict):
            continue
        nested = parent.get("rate_limit")
        if isinstance(nested, dict):
            candidates.append((f"{parent_key}.rate_limit", nested))

    return candidates


def _extract_quota_from_rate_limit(window: str, payloads: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    对齐 cockpit：优先 wham/usage.rate_limit；缺失时使用 codex/usage.rate_limit 兜底。
    """
    for source_name in ("wham_usage", "codex_usage"):
        payload = payloads.get(source_name) or {}
        if not isinstance(payload, dict):
            continue

        for source_path, rate_limit in _iter_rate_limit_candidates(payload):
            selected = _select_rate_limit_window(rate_limit, window)
            if not selected:
                continue
            selected_key, selected_payload = selected
            parsed = _extract_quota_from_rate_limit_window(selected_payload)
            if parsed:
                parsed["source"] = f"{source_name}.{source_path}.{selected_key}"
                return parsed

        # 兼容某些接口直接把 primary/secondary_window 放在顶层
        top_level_rate_limit = {
            "primary_window": payload.get("primary_window"),
            "secondary_window": payload.get("secondary_window"),
        }
        selected_direct = _select_rate_limit_window(top_level_rate_limit, window)
        if not selected_direct:
            continue
        selected_key, selected_payload = selected_direct
        parsed_direct = _extract_quota_from_rate_limit_window(selected_payload)
        if parsed_direct:
            parsed_direct["source"] = f"{source_name}.{selected_key}"
            return parsed_direct
    return None


def _extract_code_review_quota(payloads: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """
    Code Review 配额：优先读取 wham/usage.code_review_rate_limit 的 primary/secondary_window。
    """
    payload = payloads.get("wham_usage") or {}
    if not isinstance(payload, dict):
        return {
            "used": None,
            "total": None,
            "remaining": None,
            "percentage": None,
            "reset_at": None,
            "reset_in_text": "-",
            "status": "unknown",
        }

    review_rate_limit = payload.get("code_review_rate_limit")
    if isinstance(review_rate_limit, dict):
        for window_key in ("primary_window", "secondary_window"):
            parsed = _extract_quota_from_rate_limit_window(review_rate_limit.get(window_key) or {})
            if parsed:
                return parsed

    return {
        "used": None,
        "total": None,
        "remaining": None,
        "percentage": None,
        "reset_at": None,
        "reset_in_text": "-",
        "status": "unknown",
    }


def _extract_quota_candidate(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    used = _pick_number(payload, _NUMERIC_KEYS_USED)
    total = _pick_number(payload, _NUMERIC_KEYS_TOTAL)
    remaining = _pick_number(payload, _NUMERIC_KEYS_REMAINING)
    percentage = _pick_number(payload, _NUMERIC_KEYS_PERCENT)

    reset_at: Optional[datetime] = None
    for key in _RESET_AT_KEYS:
        if key in payload:
            reset_at = _normalize_datetime(payload.get(key))
            if reset_at:
                break

    reset_in_seconds = None
    for key in _RESET_IN_KEYS:
        if key in payload:
            reset_in_seconds = _to_float(payload.get(key))
            if reset_in_seconds is not None:
                break

    if remaining is None and total is not None and used is not None:
        remaining = max(total - used, 0)
    if used is None and total is not None and remaining is not None:
        used = max(total - remaining, 0)

    if percentage is not None:
        # 兼容 0~1 的比例值
        if 0 <= percentage <= 1:
            percentage *= 100
    elif total and total > 0 and remaining is not None:
        percentage = (remaining / total) * 100

    if reset_at is None and reset_in_seconds is not None:
        reset_at = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=int(reset_in_seconds))

    if all(v is None for v in (used, total, remaining, percentage, reset_at, reset_in_seconds)):
        return None

    percentage = max(0, min(float(percentage or 0), 100))
    reset_in_text = _format_duration(reset_in_seconds)
    if reset_in_seconds is None and reset_at:
        delta = (reset_at - datetime.now(timezone.utc)).total_seconds()
        reset_in_text = _format_duration(delta)

    return {
        "used": int(used) if used is not None else None,
        "total": int(total) if total is not None else None,
        "remaining": int(remaining) if remaining is not None else None,
        "percentage": round(percentage, 2) if percentage is not None else None,
        "reset_at": reset_at.isoformat() if reset_at else None,
        "reset_in_text": reset_in_text,
        "status": "ok",
    }


def _walk_candidates(payload: Any, window: str, prefix: str = "") -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    if isinstance(payload, dict):
        if _detect_window_match(prefix, payload, window):
            parsed = _extract_quota_candidate(payload)
            if parsed:
                candidates.append(parsed)
        for key, value in payload.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            candidates.extend(_walk_candidates(value, window, child_prefix))
    elif isinstance(payload, list):
        for idx, item in enumerate(payload):
            child_prefix = f"{prefix}[{idx}]"
            candidates.extend(_walk_candidates(item, window, child_prefix))
    return candidates


def _extract_quota(window: str, payloads: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    # 先按 cockpit-tools 的核心结构解析：rate_limit.primary/secondary_window.used_percent
    # primary=5小时窗口，secondary=周窗口。
    strict = _extract_quota_from_rate_limit(window, payloads)
    if strict:
        return strict

    return {
        "used": None,
        "total": None,
        "remaining": None,
        "percentage": None,
        "reset_at": None,
        "reset_in_text": "-",
        "status": "unknown",
    }


def _normalize_plan(raw_plan: Optional[str]) -> str:
    plan = (raw_plan or "").strip().lower()
    if not plan:
        return "Basic"
    if "enterprise" in plan or "team" in plan:
        return "Team"
    if "plus" in plan:
        return "Plus"
    if "pro" in plan:
        return "Pro"
    if "free" in plan or "basic" in plan:
        return "Basic"
    return plan.capitalize()


def _extract_plan_string_candidates(me_payload: Dict[str, Any]) -> List[str]:
    """
    提取常见 plan 字段的字符串候选值。
    """
    candidates: List[str] = []

    def _append(value: Any):
        if isinstance(value, str):
            text = value.strip()
            if text:
                candidates.append(text)

    _append(me_payload.get("plan_type"))
    _append(me_payload.get("plan"))
    _append(me_payload.get("subscription_plan"))
    _append(me_payload.get("account_plan"))
    _append(me_payload.get("subscription_tier"))
    _append(me_payload.get("chatgpt_plan_type"))
    _append(me_payload.get("tier"))
    _append(me_payload.get("planType"))

    account_block = me_payload.get("account")
    if isinstance(account_block, dict):
        _append(account_block.get("plan_type"))
        _append(account_block.get("plan"))
        _append(account_block.get("subscription_plan"))
        _append(account_block.get("subscription_tier"))
        _append(account_block.get("chatgpt_plan_type"))

    subscription_block = me_payload.get("subscription")
    if isinstance(subscription_block, dict):
        _append(subscription_block.get("plan_type"))
        _append(subscription_block.get("plan"))
        _append(subscription_block.get("product"))
        _append(subscription_block.get("tier"))

    return candidates


def _detect_plan_from_payload(payload: Dict[str, Any], source_name: str) -> Optional[Tuple[str, str]]:
    if not isinstance(payload, dict) or not payload:
        return None
    for raw in _extract_plan_string_candidates(payload):
        normalized = _normalize_plan(raw)
        if normalized in ("Team", "Plus", "Pro", "Basic"):
            return normalized, f"{source_name}.plan"
    return None


def _detect_plan(account: Account, payloads: Dict[str, Dict[str, Any]]) -> Tuple[str, str]:
    me = payloads.get("me") or {}
    if isinstance(me, dict) and me:
        # 1) 直接 plan 字段优先
        for raw in _extract_plan_string_candidates(me):
            normalized = _normalize_plan(raw)
            if normalized in ("Team", "Plus", "Pro"):
                return normalized, "me.plan"
            if normalized == "Basic" and raw.strip():
                # 明确返回 free/basic 也算有效信号
                return normalized, "me.plan"

        # 2) org/workspace 信息判断 Team
        orgs = []
        org_block = me.get("orgs")
        if isinstance(org_block, dict):
            orgs = org_block.get("data") or []
        if isinstance(orgs, list):
            for org in orgs:
                if not isinstance(org, dict):
                    continue
                settings_ = org.get("settings")
                if isinstance(settings_, dict):
                    workspace_plan_type = str(settings_.get("workspace_plan_type") or "").strip().lower()
                    if workspace_plan_type in ("team", "enterprise"):
                        return "Team", "me.org.workspace_plan_type"
                org_plan = _normalize_plan(str(org.get("plan_type") or org.get("plan") or ""))
                if org_plan in ("Team", "Plus", "Pro"):
                    return org_plan, "me.org.plan"

        # 3) 订阅布尔信号（无法区分 Team/Plus 时按 Plus 处理）
        bool_markers = (
            me.get("has_paid_subscription"),
            me.get("has_active_subscription"),
            me.get("is_paid"),
            me.get("is_subscribed"),
        )
        if any(v is True for v in bool_markers):
            return "Plus", "me.subscription_flag"

    # Cockpit-tools 同款核心信号：wham/usage 的 plan_type
    for source_name in ("wham_usage", "codex_usage"):
        detected = _detect_plan_from_payload(payloads.get(source_name) or {}, source_name)
        if detected:
            return detected

    # JWT claim 兜底（不依赖刷新 token）
    for source_name, token in (
        ("id_token.chatgpt_plan_type", account.id_token),
        ("access_token.chatgpt_plan_type", account.access_token),
    ):
        normalized = _extract_chatgpt_plan_from_jwt(token)
        if normalized in ("Team", "Plus", "Pro", "Basic"):
            return normalized, source_name

    # fallback 到数据库订阅字段
    if account.subscription_type:
        return _normalize_plan(account.subscription_type), "db.subscription_type"
    return "Basic", "default"


def fetch_codex_overview(account: Account, proxy: Optional[str] = None) -> Dict[str, Any]:
    """
    从 OpenAI 接口抓取 Codex 账号概览（计划类型 + 配额）
    """
    if not account.access_token:
        raise ValueError("账号缺少 access_token")

    headers = _build_headers(account)
    payloads: Dict[str, Dict[str, Any]] = {}
    errors: List[str] = []

    for source_name, url, required in _USAGE_ENDPOINTS:
        try:
            payloads[source_name] = _request_json_with_proxy_fallback(url, headers, proxy)
        except Exception as exc:
            status = _extract_http_status(exc)
            # 可选端点在 401/403/404 场景静默降级，避免影响总览刷新日志可读性。
            if not required and status in (401, 403, 404):
                logger.debug("概览可选端点降级跳过: source=%s status=%s", source_name, status)
                continue
            errors.append(f"{source_name}: {exc}")

    if not payloads:
        raise RuntimeError("所有概览接口请求失败")

    plan_type, plan_source = _detect_plan(account, payloads)
    hourly_quota = _extract_quota("hourly", payloads)
    weekly_quota = _extract_quota("weekly", payloads)
    code_review_quota = _extract_code_review_quota(payloads)

    return {
        "plan_type": plan_type,
        "plan_source": plan_source,
        "hourly_quota": hourly_quota,
        "weekly_quota": weekly_quota,
        "code_review_quota": code_review_quota,
        "sources": list(payloads.keys()),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "errors": errors[:3],
    }
