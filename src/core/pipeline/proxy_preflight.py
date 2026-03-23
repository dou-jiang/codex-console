from __future__ import annotations

import random
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Iterable

from sqlalchemy.orm import Session

from src.core.http_client import HTTPClient
from src.database import crud
from src.database.models import Proxy, ProxyCheckRun

ProxyRow = dict[str, Any]
ProxyChecker = Callable[[ProxyRow], dict[str, Any]]

_DEFAULT_PROXY_TEST_URL = "https://api.ipify.org?format=json"


def run_proxy_preflight(
    db: Session,
    *,
    scope_type: str,
    scope_id: str | None,
    proxies: Iterable[ProxyRow | Proxy],
    check_single_proxy: ProxyChecker | None = None,
) -> tuple[ProxyCheckRun, list[ProxyRow]]:
    proxy_rows = [_normalize_proxy_row(proxy) for proxy in proxies]
    check_single_proxy = check_single_proxy or _default_check_single_proxy

    run = crud.create_proxy_check_run(
        db,
        scope_type=scope_type,
        scope_id=scope_id,
        status="running",
        total_count=len(proxy_rows),
        available_count=0,
    )

    with ThreadPoolExecutor(max_workers=min(32, len(proxy_rows) or 1)) as pool:
        futures = [pool.submit(check_single_proxy, proxy) for proxy in proxy_rows]
        checked_rows = [_merge_probe_result(proxy, future) for proxy, future in zip(proxy_rows, futures)]

    available_count = 0
    for item in checked_rows:
        if item["status"] == "available":
            available_count += 1
        crud.create_proxy_check_result(
            db,
            proxy_check_run_id=run.id,
            proxy_id=item.get("proxy_id"),
            proxy_url=item.get("proxy_url"),
            status=item["status"],
            latency_ms=item.get("latency_ms"),
            country_code=item.get("country_code"),
            ip_address=item.get("ip_address"),
            error_message=item.get("error_message"),
        )

    run = crud.finalize_proxy_check_run(
        db,
        run.id,
        status="completed",
        total_count=len(proxy_rows),
        available_count=available_count,
    ) or run

    return run, checked_rows


def choose_available_proxy(results: list[dict[str, Any]]) -> dict[str, Any]:
    available = [item for item in results if item["status"] == "available"]
    if not available:
        raise RuntimeError("no available proxy")
    return random.choice(available)


def _normalize_proxy_row(proxy: ProxyRow | Proxy) -> ProxyRow:
    if isinstance(proxy, dict):
        row = dict(proxy)
    else:
        row = {
            "proxy_id": proxy.id,
            "proxy_url": proxy.proxy_url,
        }

    if "proxy_id" not in row and "id" in row:
        row["proxy_id"] = row["id"]
    if "proxy_url" not in row and "url" in row:
        row["proxy_url"] = row["url"]

    row.setdefault("proxy_id", None)
    row.setdefault("proxy_url", None)
    return row


def _merge_probe_result(proxy_row: ProxyRow, future: Future[dict[str, Any]]) -> ProxyRow:
    try:
        payload = future.result()
    except Exception as exc:
        payload = {"status": "unavailable", "error_message": str(exc)}

    result_row = dict(proxy_row)
    normalized_payload = _normalize_probe_payload(payload)
    result_row.update(normalized_payload)
    return result_row


def _normalize_probe_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = dict(payload or {})
    status = "available" if data.get("status") == "available" else "unavailable"

    normalized: dict[str, Any] = {"status": status}

    latency_ms = data.get("latency_ms")
    if latency_ms is not None:
        try:
            normalized["latency_ms"] = int(latency_ms)
        except (TypeError, ValueError):
            pass

    for key in ("country_code", "ip_address", "error_message"):
        value = data.get(key)
        if value is not None:
            normalized[key] = str(value)

    return normalized


def _default_check_single_proxy(proxy_row: ProxyRow) -> dict[str, Any]:
    proxy_url = str(proxy_row.get("proxy_url") or "").strip()
    if not proxy_url:
        return {"status": "unavailable", "error_message": "missing proxy_url"}

    started_at = time.perf_counter()
    try:
        with HTTPClient(proxy_url=proxy_url) as client:
            response = client.get(_DEFAULT_PROXY_TEST_URL, timeout=10)
            response.raise_for_status()
            payload = response.json() if hasattr(response, "json") else {}
        latency_ms = max(0, int((time.perf_counter() - started_at) * 1000))
        return {
            "status": "available",
            "latency_ms": latency_ms,
            "ip_address": (payload or {}).get("ip"),
        }
    except Exception as exc:
        latency_ms = max(0, int((time.perf_counter() - started_at) * 1000))
        return {
            "status": "unavailable",
            "latency_ms": latency_ms,
            "error_message": str(exc),
        }
