from __future__ import annotations

from typing import Any

from curl_cffi import requests as cffi_requests

from ..core.upload.cpa_upload import _normalize_cpa_auth_files_url


def _service_value(service: Any, key: str) -> str:
    if isinstance(service, dict):
        value = service.get(key)
    else:
        value = getattr(service, key, None)
    return str(value or "").strip()


def _build_endpoint(service: Any) -> str:
    endpoint = _normalize_cpa_auth_files_url(_service_value(service, "api_url"))
    if not endpoint:
        raise ValueError("cpa service api_url is required")
    return endpoint


def _build_headers(service: Any) -> dict[str, str]:
    token = _service_value(service, "api_token")
    if not token:
        raise ValueError("cpa service api_token is required")
    return {"Authorization": f"Bearer {token}"}


def _extract_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if isinstance(payload, dict):
        for key in ("items", "files", "data", "invalid_items", "invalid"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                return [item for item in candidate if isinstance(item, dict)]

    return []


def _safe_json(response: Any) -> Any:
    try:
        return response.json()
    except Exception:
        return None


def _raise_for_http_error(response: Any, action: str) -> None:
    if 200 <= response.status_code < 300:
        return
    raise RuntimeError(f"CPA {action} failed: HTTP {response.status_code}")


def _is_invalid_item(item: dict[str, Any]) -> bool:
    if "invalid" in item:
        return bool(item.get("invalid"))
    if "is_valid" in item:
        return not bool(item.get("is_valid"))

    status = str(item.get("status") or "").lower()
    if status:
        return status in {"invalid", "expired", "disabled", "dead"}

    return True


def count_valid_accounts(service, *, timeout: int = 20) -> int:
    endpoint = _build_endpoint(service)
    response = cffi_requests.get(
        endpoint,
        headers=_build_headers(service),
        timeout=timeout,
        proxies=None,
        impersonate="chrome110",
    )
    _raise_for_http_error(response, "count")

    payload = _safe_json(response)
    if isinstance(payload, dict):
        for key in ("valid_count", "count", "total", "total_count"):
            value = payload.get(key)
            if isinstance(value, int):
                return max(0, value)

    items = _extract_items(payload)
    if not items:
        return 0

    explicit_validity = any(("invalid" in item) or ("is_valid" in item) or ("status" in item) for item in items)
    if explicit_validity:
        return sum(1 for item in items if not _is_invalid_item(item))
    return len(items)


def probe_invalid_accounts(service, *, limit: int | None = None, timeout: int = 20) -> list[dict[str, Any]]:
    endpoint = _build_endpoint(service)
    params: dict[str, Any] = {"invalid": "1"}
    if isinstance(limit, int) and limit > 0:
        params["limit"] = limit

    response = cffi_requests.get(
        endpoint,
        params=params,
        headers=_build_headers(service),
        timeout=timeout,
        proxies=None,
        impersonate="chrome110",
    )

    if response.status_code == 404:
        response = cffi_requests.get(
            endpoint,
            headers=_build_headers(service),
            timeout=timeout,
            proxies=None,
            impersonate="chrome110",
        )

    _raise_for_http_error(response, "probe")
    payload = _safe_json(response)
    items = _extract_items(payload)

    normalized: list[dict[str, Any]] = []
    for item in items:
        if any(k in item for k in ("invalid", "is_valid", "status")) and not _is_invalid_item(item):
            continue

        email = str(item.get("email") or "").strip()
        name = str(item.get("name") or item.get("filename") or "").strip()

        if not email and name.endswith(".json"):
            email = name[:-5]
        if not name and email:
            name = f"{email}.json"

        if not email:
            continue

        normalized.append({"email": email, "name": name})

    if isinstance(limit, int) and limit > 0:
        return normalized[:limit]
    return normalized


def _extract_delete_counts(payload: Any, total: int) -> dict[str, int]:
    if not isinstance(payload, dict):
        return {"deleted": 0, "failed": total}

    deleted_raw = payload.get("deleted", payload.get("success_count", payload.get("success")))
    failed_raw = payload.get("failed", payload.get("failed_count", payload.get("error_count")))

    if isinstance(deleted_raw, list):
        deleted = len(deleted_raw)
    elif isinstance(deleted_raw, int):
        deleted = deleted_raw
    else:
        deleted = 0

    if isinstance(failed_raw, list):
        failed = len(failed_raw)
    elif isinstance(failed_raw, int):
        failed = failed_raw
    else:
        failed = max(0, total - deleted)

    return {"deleted": max(0, deleted), "failed": max(0, failed)}


def delete_invalid_accounts(service, names: list[str], *, timeout: int = 20) -> dict[str, int]:
    cleaned_names = [name for name in names if isinstance(name, str) and name.strip()]
    if not cleaned_names:
        return {"deleted": 0, "failed": 0}

    endpoint = _build_endpoint(service)
    headers = _build_headers(service)
    payload = {"names": cleaned_names}

    response = cffi_requests.delete(
        endpoint,
        json=payload,
        headers=headers,
        timeout=timeout,
        proxies=None,
        impersonate="chrome110",
    )

    if response.status_code in (404, 405):
        response = cffi_requests.post(
            f"{endpoint}/delete",
            json=payload,
            headers=headers,
            timeout=timeout,
            proxies=None,
            impersonate="chrome110",
        )

    _raise_for_http_error(response, "delete")
    return _extract_delete_counts(_safe_json(response), total=len(cleaned_names))
