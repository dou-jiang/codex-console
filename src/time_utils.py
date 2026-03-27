"""Shared time helpers for UTC-naive timestamps."""

from datetime import UTC, datetime


def utc_now_naive() -> datetime:
    """Return a naive datetime representing current UTC time."""
    return datetime.now(UTC).replace(tzinfo=None)
