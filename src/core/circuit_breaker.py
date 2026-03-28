"""
轻量级失败熔断器（DB 持久化 + 自动冷却探活恢复）。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from ..config.settings import get_settings
from ..database import crud
from ..database.session import get_db

BREAKER_SETTING_KEY = "runtime.circuit_breaker.v1"
BREAKER_CHANNELS = ("proxy_runtime", "subscription_check", "team_invite")
_CACHE_TTL_SECONDS = 2.0

_state_lock = threading.Lock()
_state_cache: Dict[str, Any] = {"loaded_ts": 0.0, "data": {}}


def _utc_now() -> datetime:
    return datetime.utcnow()


def _now_iso() -> str:
    return _utc_now().isoformat()


def _parse_dt(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _settings_config() -> Dict[str, Any]:
    settings = get_settings()
    enabled = bool(getattr(settings, "circuit_breaker_enabled", True))
    threshold = max(1, _safe_int(getattr(settings, "circuit_breaker_failure_threshold", 5), 5))
    cooldown_seconds = max(10, _safe_int(getattr(settings, "circuit_breaker_cooldown_seconds", 180), 180))
    probe_interval_seconds = max(3, _safe_int(getattr(settings, "circuit_breaker_probe_interval_seconds", 30), 30))
    return {
        "enabled": enabled,
        "failure_threshold": threshold,
        "cooldown_seconds": cooldown_seconds,
        "probe_interval_seconds": probe_interval_seconds,
    }


def _default_entry() -> Dict[str, Any]:
    return {
        "consecutive_fail": 0,
        "opened_until": None,
        "last_failure_at": None,
        "last_success_at": None,
        "last_error": None,
        "last_probe_at": None,
        "open_count": 0,
    }


def _normalize_state(raw: Any) -> Dict[str, Dict[str, Any]]:
    state = raw if isinstance(raw, dict) else {}
    result: Dict[str, Dict[str, Any]] = {}
    for channel in BREAKER_CHANNELS:
        entry = state.get(channel)
        merged = _default_entry()
        if isinstance(entry, dict):
            merged.update(entry)
        result[channel] = merged
    return result


def _load_state(force: bool = False) -> Dict[str, Dict[str, Any]]:
    now_ts = _utc_now().timestamp()
    with _state_lock:
        if (not force) and (now_ts - float(_state_cache.get("loaded_ts") or 0.0) <= _CACHE_TTL_SECONDS):
            return _normalize_state(_state_cache.get("data"))

        with get_db() as db:
            setting = crud.get_setting(db, BREAKER_SETTING_KEY)
            raw_text = str(getattr(setting, "value", "") or "").strip()
            try:
                parsed = json.loads(raw_text) if raw_text else {}
            except Exception:
                parsed = {}
        normalized = _normalize_state(parsed)
        _state_cache["loaded_ts"] = now_ts
        _state_cache["data"] = normalized
        return _normalize_state(normalized)


def _save_state(state: Dict[str, Dict[str, Any]]) -> None:
    safe_state = _normalize_state(state)
    payload = json.dumps(safe_state, ensure_ascii=False)
    with _state_lock:
        with get_db() as db:
            crud.set_setting(
                db,
                key=BREAKER_SETTING_KEY,
                value=payload,
                description="失败熔断器运行时状态",
                category="runtime",
            )
        _state_cache["loaded_ts"] = _utc_now().timestamp()
        _state_cache["data"] = safe_state


def _ensure_channel(channel: str) -> str:
    name = str(channel or "").strip().lower()
    if name not in BREAKER_CHANNELS:
        raise ValueError(f"unsupported breaker channel: {channel}")
    return name


def allow_request(channel: str) -> Tuple[bool, Dict[str, Any]]:
    name = _ensure_channel(channel)
    cfg = _settings_config()
    if not cfg["enabled"]:
        return True, {"state": "disabled"}

    state = _load_state()
    entry = state[name]
    now = _utc_now()
    opened_until = _parse_dt(entry.get("opened_until"))
    if opened_until and opened_until > now:
        return False, {
            "state": "open",
            "opened_until": opened_until.isoformat(),
            "consecutive_fail": _safe_int(entry.get("consecutive_fail"), 0),
        }

    if opened_until and opened_until <= now:
        last_probe = _parse_dt(entry.get("last_probe_at"))
        if last_probe and (now - last_probe).total_seconds() < float(cfg["probe_interval_seconds"]):
            next_probe_at = last_probe + timedelta(seconds=float(cfg["probe_interval_seconds"]))
            return False, {
                "state": "half_open_wait",
                "opened_until": opened_until.isoformat(),
                "next_probe_at": next_probe_at.isoformat(),
                "consecutive_fail": _safe_int(entry.get("consecutive_fail"), 0),
            }
        entry["last_probe_at"] = _now_iso()
        state[name] = entry
        _save_state(state)
        return True, {"state": "half_open_probe", "opened_until": opened_until.isoformat()}

    return True, {"state": "closed"}


def record_success(channel: str) -> Dict[str, Any]:
    name = _ensure_channel(channel)
    state = _load_state()
    entry = state[name]
    entry["consecutive_fail"] = 0
    entry["opened_until"] = None
    entry["last_success_at"] = _now_iso()
    entry["last_error"] = None
    entry["last_probe_at"] = None
    state[name] = entry
    _save_state(state)
    return dict(entry)


def record_failure(channel: str, error_message: Optional[str] = None) -> Dict[str, Any]:
    name = _ensure_channel(channel)
    cfg = _settings_config()
    state = _load_state()
    entry = state[name]
    now = _utc_now()
    consecutive = _safe_int(entry.get("consecutive_fail"), 0) + 1
    entry["consecutive_fail"] = consecutive
    entry["last_failure_at"] = now.isoformat()
    entry["last_error"] = str(error_message or "").strip()[:500] or None

    if cfg["enabled"] and consecutive >= int(cfg["failure_threshold"]):
        entry["opened_until"] = (now + timedelta(seconds=int(cfg["cooldown_seconds"]))).isoformat()
        entry["open_count"] = _safe_int(entry.get("open_count"), 0) + 1

    state[name] = entry
    _save_state(state)
    return dict(entry)


def reset_channel(channel: str) -> Dict[str, Any]:
    name = _ensure_channel(channel)
    state = _load_state()
    state[name] = _default_entry()
    state[name]["last_success_at"] = _now_iso()
    _save_state(state)
    return dict(state[name])


def snapshot() -> Dict[str, Any]:
    return {
        "config": _settings_config(),
        "channels": _load_state(),
    }
