"""Shared OTP candidate filtering and ranking helpers."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from ..config.constants import OTP_CODE_SEMANTIC_PATTERN


def parse_mail_timestamp(value: Any) -> Optional[float]:
    """Parse provider-specific time values into a Unix timestamp."""
    if value is None:
        return None

    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10**12:
            timestamp = timestamp / 1000.0
        return timestamp if timestamp > 0 else None

    text = str(value).strip()
    if not text:
        return None

    if text.isdigit():
        timestamp = float(text)
        if timestamp > 10**12:
            timestamp = timestamp / 1000.0
        return timestamp if timestamp > 0 else None

    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text

    try:
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue

    return None


def is_openai_otp_text(*parts: str) -> bool:
    """Return True when the mail content looks like an OpenAI OTP mail."""
    blob = "\n".join(str(part or "") for part in parts).lower()
    if "openai" not in blob:
        return False

    otp_keywords = (
        "verification",
        "verification code",
        "verify",
        "one-time code",
        "one time code",
        "otp",
        "log in",
        "login",
        "security code",
        "验证码",
    )
    return any(keyword in blob for keyword in otp_keywords)


def extract_otp_code(content: str, pattern: str) -> tuple[Optional[str], bool]:
    """Extract an OTP code and indicate whether a semantic match was used."""
    text = str(content or "")
    if not text:
        return None, False

    semantic_match = re.search(OTP_CODE_SEMANTIC_PATTERN, text, re.IGNORECASE)
    if semantic_match:
        return semantic_match.group(1), True

    generic_match = re.search(pattern, text)
    if generic_match:
        return generic_match.group(1), False

    return None, False


def select_best_otp_candidate(
    candidates: Iterable[dict[str, Any]],
    *,
    otp_sent_at: Optional[float] = None,
    last_used_mail_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Choose the safest OTP candidate from a provider-specific candidate list."""
    filtered: list[dict[str, Any]] = []
    for candidate in candidates:
        mail_id = str(candidate.get("mail_id") or "").strip()
        if last_used_mail_id and mail_id == str(last_used_mail_id):
            continue

        mail_ts = candidate.get("mail_ts")
        if otp_sent_at and mail_ts is not None and float(mail_ts) + 2 < float(otp_sent_at):
            continue

        filtered.append(candidate)

    if not filtered:
        return None

    return sorted(
        filtered,
        key=lambda item: (
            1 if item.get("mail_ts") is not None else 0,
            1 if item.get("semantic_hit") else 0,
            float(item.get("mail_ts") or 0.0),
        ),
        reverse=True,
    )[0]
