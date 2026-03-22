from __future__ import annotations

from src.scheduler import cpa_client


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _service():
    return {
        "api_url": "https://cpa.example.com/v0/management",
        "api_token": "token-123",
    }


def test_probe_invalid_accounts_skips_generic_auth_file_list_without_invalid_markers(monkeypatch):
    responses = [
        FakeResponse(status_code=404, payload={"message": "unsupported filter"}),
        FakeResponse(
            status_code=200,
            payload=[
                {"email": "a@example.com", "name": "a@example.com.json"},
                {"email": "b@example.com", "name": "b@example.com.json"},
            ],
        ),
    ]

    def _fake_get(url, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(cpa_client.cffi_requests, "get", _fake_get)

    assert cpa_client.probe_invalid_accounts(_service()) == []


def test_probe_invalid_accounts_returns_only_items_with_positive_invalid_markers(monkeypatch):
    payload = {
        "items": [
            {"email": "expired@example.com", "name": "expired@example.com.json", "status": "expired"},
            {"name": "disabled@example.com.json", "disabled": True},
            {"email": "active@example.com", "name": "active@example.com.json", "status": "active"},
            {"email": "plain@example.com", "name": "plain@example.com.json"},
        ]
    }

    monkeypatch.setattr(cpa_client.cffi_requests, "get", lambda url, **kwargs: FakeResponse(status_code=200, payload=payload))

    assert cpa_client.probe_invalid_accounts(_service()) == [
        {"email": "expired@example.com", "name": "expired@example.com.json"},
        {"email": "disabled@example.com", "name": "disabled@example.com.json"},
    ]
