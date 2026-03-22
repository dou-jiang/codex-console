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


def test_probe_invalid_accounts_falls_back_to_api_call_401_probe_when_filter_payload_has_no_invalid_markers(monkeypatch):
    payload = {
        "files": [
            {
                "email": "active@example.com",
                "name": "active@example.com.json",
                "type": "codex",
                "auth_index": "auth-active",
                "account_id": "acct-active",
            },
            {
                "email": "invalid@example.com",
                "name": "invalid@example.com.json",
                "type": "codex",
                "auth_index": "auth-invalid",
                "account_id": "acct-invalid",
            },
        ]
    }

    monkeypatch.setattr(cpa_client.cffi_requests, "get", lambda url, **kwargs: FakeResponse(status_code=200, payload=payload))

    post_calls = []

    def _fake_post(url, **kwargs):
        post_calls.append({"url": url, "json": kwargs.get("json")})
        auth_index = kwargs["json"]["authIndex"]
        status_code = 401 if auth_index == "auth-invalid" else 200
        return FakeResponse(status_code=200, payload={"status_code": status_code})

    monkeypatch.setattr(cpa_client.cffi_requests, "post", _fake_post)

    assert cpa_client.probe_invalid_accounts(_service()) == [
        {"email": "invalid@example.com", "name": "invalid@example.com.json"}
    ]
    assert len(post_calls) == 2
    assert post_calls[0]["url"] == "https://cpa.example.com/v0/management/api-call"
    assert post_calls[0]["json"]["header"]["Chatgpt-Account-Id"] == "acct-active"


def test_probe_invalid_accounts_limit_caps_number_of_401_results(monkeypatch):
    payload = {
        "files": [
            {
                "email": "invalid1@example.com",
                "name": "invalid1@example.com.json",
                "type": "codex",
                "auth_index": "auth-1",
            },
            {
                "email": "invalid2@example.com",
                "name": "invalid2@example.com.json",
                "type": "codex",
                "auth_index": "auth-2",
            },
            {
                "email": "invalid3@example.com",
                "name": "invalid3@example.com.json",
                "type": "codex",
                "auth_index": "auth-3",
            },
        ]
    }

    monkeypatch.setattr(cpa_client.cffi_requests, "get", lambda url, **kwargs: FakeResponse(status_code=200, payload=payload))

    post_calls = []

    def _fake_post(url, **kwargs):
        post_calls.append(kwargs["json"]["authIndex"])
        return FakeResponse(status_code=200, payload={"status_code": 401})

    monkeypatch.setattr(cpa_client.cffi_requests, "post", _fake_post)

    assert cpa_client.probe_invalid_accounts(_service(), limit=2) == [
        {"email": "invalid1@example.com", "name": "invalid1@example.com.json"},
        {"email": "invalid2@example.com", "name": "invalid2@example.com.json"},
    ]
    assert post_calls == ["auth-1", "auth-2"]


def test_probe_invalid_accounts_reads_chatgpt_account_id_from_nested_id_token(monkeypatch):
    payload = {
        "files": [
            {
                "email": "invalid@example.com",
                "name": "invalid@example.com.json",
                "type": "codex",
                "auth_index": "auth-invalid",
                "id_token": {"chatgpt_account_id": "acct-nested"},
            },
        ]
    }

    monkeypatch.setattr(cpa_client.cffi_requests, "get", lambda url, **kwargs: FakeResponse(status_code=200, payload=payload))

    captured = {}

    def _fake_post(url, **kwargs):
        captured["json"] = kwargs["json"]
        return FakeResponse(status_code=200, payload={"status_code": 401})

    monkeypatch.setattr(cpa_client.cffi_requests, "post", _fake_post)

    assert cpa_client.probe_invalid_accounts(_service(), limit=1) == [
        {"email": "invalid@example.com", "name": "invalid@example.com.json"},
    ]
    assert captured["json"]["header"]["Chatgpt-Account-Id"] == "acct-nested"


def test_delete_invalid_accounts_uses_query_name_delete_endpoint(monkeypatch):
    delete_calls = []

    def _fake_delete(url, **kwargs):
        delete_calls.append(url)
        return FakeResponse(status_code=200, payload={"status": "ok"})

    monkeypatch.setattr(cpa_client.cffi_requests, "delete", _fake_delete)

    result = cpa_client.delete_invalid_accounts(
        _service(),
        ["a@example.com.json", "b@example.com.json"],
    )

    assert result == {"deleted": 2, "failed": 0}
    assert delete_calls == [
        "https://cpa.example.com/v0/management/auth-files?name=a%40example.com.json",
        "https://cpa.example.com/v0/management/auth-files?name=b%40example.com.json",
    ]


def test_delete_invalid_accounts_counts_failures_per_name(monkeypatch):
    responses = [
        FakeResponse(status_code=200, payload={"status": "ok"}),
        FakeResponse(status_code=400, payload={"error": "invalid name"}),
    ]

    monkeypatch.setattr(cpa_client.cffi_requests, "delete", lambda url, **kwargs: responses.pop(0))

    result = cpa_client.delete_invalid_accounts(
        _service(),
        ["ok@example.com.json", "bad@example.com.json"],
    )

    assert result == {"deleted": 1, "failed": 1}
