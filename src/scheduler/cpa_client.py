from __future__ import annotations

from typing import Any

from curl_cffi import requests as cffi_requests


_INVALID_TEXT_HINTS = (
    "invalid",
    "expired",
    "401",
    "unauthorized",
    "disabled",
    "unusable",
)

_VALIDITY_MARKER_KEYS = {
    "invalid",
    "is_valid",
    "valid",
    "expired",
    "disabled",
    "usable",
    "status",
    "state",
    "account_status",
    "result",
    "http_status",
    "status_code",
    "code",
    "error_code",
    "reason",
    "error",
    "message",
    "detail",
}


def _normalize_auth_files_url(api_url: str) -> str:
    normalized = (api_url or "").strip().rstrip("/")
    lower_url = normalized.lower()

    if not normalized:
        return ""

    if lower_url.endswith("/auth-files"):
        return normalized

    if lower_url.endswith("/v0/management") or lower_url.endswith("/management"):
        return f"{normalized}/auth-files"

    if lower_url.endswith("/v0"):
        return f"{normalized}/management/auth-files"

    return f"{normalized}/v0/management/auth-files"


def _service_value(service: Any, key: str) -> str:
    if isinstance(service, dict):
        value = service.get(key)
    else:
        value = getattr(service, key, None)
    return str(value or "").strip()


def _build_endpoint(service: Any) -> str:
    endpoint = _normalize_auth_files_url(_service_value(service, "api_url"))
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


def _normalize_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return value != 0

    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off", ""}:
            return False

    return None


def _contains_invalid_text_hint(value: Any) -> bool:
    text = _normalize_text(value)
    if not text:
        return False
    return any(hint in text for hint in _INVALID_TEXT_HINTS)


def _has_positive_invalid_evidence(item: dict[str, Any]) -> bool:
    for key in ("invalid", "expired", "disabled"):
        if key in item:
            parsed = _as_bool(item.get(key))
            if parsed is True:
                return True

    for key in ("is_valid", "valid", "usable"):
        if key in item:
            parsed = _as_bool(item.get(key))
            if parsed is False:
                return True

    for key in ("status", "state", "account_status", "result"):
        if key in item and _contains_invalid_text_hint(item.get(key)):
            return True

    for key in ("http_status", "status_code", "code", "error_code"):
        if key not in item:
            continue

        raw_value = item.get(key)
        if isinstance(raw_value, (int, float)) and int(raw_value) == 401:
            return True
        if _contains_invalid_text_hint(raw_value):
            return True

    for key in ("reason", "error", "message", "detail"):
        if key in item and _contains_invalid_text_hint(item.get(key)):
            return True

    return False


def _has_any_validity_marker(item: dict[str, Any]) -> bool:
    return any(key in item for key in _VALIDITY_MARKER_KEYS)


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

    if any(_has_any_validity_marker(item) for item in items):
        return sum(1 for item in items if not _has_positive_invalid_evidence(item))

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
        if not _has_positive_invalid_evidence(item):
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
