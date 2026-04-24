from src.core.upload import cpa_upload


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


class FakeMime:
    def __init__(self):
        self.parts = []

    def addpart(self, **kwargs):
        self.parts.append(kwargs)


def test_upload_to_cpa_accepts_management_root_url(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, "kwargs": kwargs})
        return FakeResponse(status_code=201)

    monkeypatch.setattr(cpa_upload, "CurlMime", FakeMime)
    monkeypatch.setattr(cpa_upload.cffi_requests, "post", fake_post)

    success, message = cpa_upload.upload_to_cpa(
        {"email": "tester@example.com"},
        api_url="https://cpa.example.com/v0/management",
        api_token="token-123",
    )

    assert success is True
    assert message == "上传成功"
    assert calls[0]["url"] == "https://cpa.example.com/v0/management/auth-files"


def test_upload_to_cpa_does_not_double_append_full_endpoint(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, "kwargs": kwargs})
        return FakeResponse(status_code=201)

    monkeypatch.setattr(cpa_upload, "CurlMime", FakeMime)
    monkeypatch.setattr(cpa_upload.cffi_requests, "post", fake_post)

    success, _ = cpa_upload.upload_to_cpa(
        {"email": "tester@example.com"},
        api_url="https://cpa.example.com/v0/management/auth-files",
        api_token="token-123",
    )

    assert success is True
    assert calls[0]["url"] == "https://cpa.example.com/v0/management/auth-files"


def test_upload_to_cpa_falls_back_to_raw_json_when_multipart_returns_404(monkeypatch):
    calls = []
    responses = [
        FakeResponse(status_code=404, text="404 page not found"),
        FakeResponse(status_code=200, payload={"status": "ok"}),
    ]

    def fake_post(url, **kwargs):
        calls.append({"url": url, "kwargs": kwargs})
        return responses.pop(0)

    monkeypatch.setattr(cpa_upload, "CurlMime", FakeMime)
    monkeypatch.setattr(cpa_upload.cffi_requests, "post", fake_post)

    success, message = cpa_upload.upload_to_cpa(
        {"email": "tester@example.com", "type": "codex"},
        api_url="https://cpa.example.com",
        api_token="token-123",
    )

    assert success is True
    assert message == "上传成功"
    assert calls[0]["kwargs"]["multipart"] is not None
    assert calls[1]["url"] == "https://cpa.example.com/v0/management/auth-files?name=tester%40example.com.json"
    assert calls[1]["kwargs"]["headers"]["Content-Type"] == "application/json"
    assert calls[1]["kwargs"]["data"].startswith(b"{")


def test_test_cpa_connection_uses_get_and_normalized_url(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append({"url": url, "kwargs": kwargs})
        return FakeResponse(status_code=200, payload={"files": []})

    monkeypatch.setattr(cpa_upload.cffi_requests, "get", fake_get)

    success, message = cpa_upload.test_cpa_connection(
        "https://cpa.example.com/v0/management",
        "token-123",
    )

    assert success is True
    assert message == "CPA 连接测试成功"
    assert calls[0]["url"] == "https://cpa.example.com/v0/management/auth-files"
    assert calls[0]["kwargs"]["headers"]["Authorization"] == "Bearer token-123"
