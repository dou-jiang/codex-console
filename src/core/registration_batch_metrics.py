from __future__ import annotations

from typing import Any, Iterable, Protocol

MISSING_EMAIL_DOMAIN = "未获取邮箱"


class TaskWithEmail(Protocol):
    status: str
    email_address: str | None


def apply_task_outcome(state: dict[str, int], status: str) -> None:
    state["completed"] = state.get("completed", 0) + 1

    if status == "completed":
        state["success"] = state.get("success", 0) + 1
        state["consecutive_failures"] = 0
    elif status == "failed":
        state["failed"] = state.get("failed", 0) + 1
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1


def build_domain_stats(tasks: Iterable[TaskWithEmail]) -> list[dict[str, float | int | str]]:
    """Return aggregated domain statistics sorted per spec."""

    aggregates: dict[str, dict[str, int]] = {}

    for task in tasks:
        status = task.status
        if status not in {"completed", "failed"}:
            continue

        domain = _normalize_domain(task.email_address)
        bucket = aggregates.setdefault(domain, {"total": 0, "success": 0, "failed": 0})
        bucket["total"] += 1

        if status == "completed":
            bucket["success"] += 1
        else:
            bucket["failed"] += 1

    stats: list[dict[str, float | int | str]] = []

    for domain, counts in aggregates.items():
        total = counts["total"]
        success = counts["success"]
        failed = counts["failed"]

        success_rate = _rate(success, total)
        failure_rate = _rate(failed, total)

        stats.append(
            {
                "domain": domain,
                "total": total,
                "success": success,
                "failed": failed,
                "success_rate": success_rate,
                "failure_rate": failure_rate,
            }
        )

    stats.sort(key=_build_sort_key)
    return stats


def _normalize_domain(email_address: str | None) -> str:
    if not email_address:
        return MISSING_EMAIL_DOMAIN

    normalized = email_address.strip().lower()
    if "@" not in normalized:
        return normalized or MISSING_EMAIL_DOMAIN

    domain = normalized.rsplit("@", 1)[-1].strip()
    return domain or MISSING_EMAIL_DOMAIN


def _rate(count: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round((count / total) * 100, 1)


def _build_sort_key(row: dict[str, Any]) -> tuple[int, float, int, str]:
    is_missing_bucket = 1 if row["domain"] == MISSING_EMAIL_DOMAIN else 0
    return (
        is_missing_bucket,
        -row["success_rate"],
        -row["total"],
        row["domain"],
    )
