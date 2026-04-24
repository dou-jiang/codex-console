from types import SimpleNamespace

from src.core.upload import new_api_upload


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


def make_account(**kwargs):
    base = {
        "id": 1,
        "email": "tester@example.com",
        "access_token": "at",
        "refresh_token": "rt",
        "session_token": "st",
        "client_id": "cid",
        "account_id": "aid",
        "workspace_id": "wid",
        "expires_at": None,
        "subscription_type": None,
        "extra_data": {},
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_resolve_new_api_account_type_returns_codex_from_extra_data():
    account = make_account(extra_data={"account_type": "codex"})
    assert new_api_upload.resolve_new_api_account_type(account) == "codex"


def test_resolve_new_api_account_type_returns_team_from_subscription():
    account = make_account(subscription_type="team")
    assert new_api_upload.resolve_new_api_account_type(account) == "team"


def test_upload_to_new_api_creates_channel_after_login(monkeypatch):
    calls = []

    class FakeSession:
        def post(self, url, **kwargs):
            calls.append({"url": url, "kwargs": kwargs})
            if url.endswith("/api/user/login"):
                response = FakeResponse(status_code=200, payload={"success": True})
                response.cookies = {"session": "cookie"}
                return response
            if url.endswith("/api/channel/"):
                return FakeResponse(status_code=200, payload={"success": True})
            raise AssertionError(url)

    def fake_create_session(api_url, username, password):
        session = FakeSession()
        session.headers = {}
        response = session.post(
            f"{new_api_upload.normalize_new_api_url(api_url)}/api/user/login",
            json={"username": username, "password": password},
        )
        response._payload = {"success": True, "data": {"id": 1}}
        return session, response

    monkeypatch.setattr(new_api_upload, "create_new_api_session", fake_create_session)

    success, message = new_api_upload.upload_to_new_api(
        [make_account(extra_data={"account_type": "codex"})],
        "https://newapi.example.com/",
        "biubush",
        "jy666666",
    )

    assert success is True
    assert "成功上传 1 个账号" == message
    assert calls[0]["url"] == "https://newapi.example.com/api/user/login"
    assert calls[1]["url"] == "https://newapi.example.com/api/channel/"
    assert calls[1]["kwargs"]["json"]["mode"] == "single"
    assert calls[1]["kwargs"]["json"]["channel"]["type"] == new_api_upload.CHANNEL_TYPE_CODEX


def test_test_new_api_connection_uses_login(monkeypatch):
    calls = []

    class FakeSession:
        def post(self, url, **kwargs):
            calls.append({"url": url, "kwargs": kwargs})
            response = FakeResponse(status_code=200, payload={"success": True})
            response.cookies = {"session": "cookie"}
            return response

    def fake_create_session(api_url, username, password):
        session = FakeSession()
        session.headers = {}
        response = session.post(
            f"{new_api_upload.normalize_new_api_url(api_url)}/api/user/login",
            json={"username": username, "password": password},
        )
        response._payload = {"success": True, "data": {"id": 1}}
        return session, response

    monkeypatch.setattr(new_api_upload, "create_new_api_session", fake_create_session)

    success, message = new_api_upload.test_new_api_connection(
        "https://newapi.example.com",
        "biubush",
        "jy666666",
    )

    assert success is True
    assert message == "new-api 连接测试成功"
    assert calls[0]["url"] == "https://newapi.example.com/api/user/login"
