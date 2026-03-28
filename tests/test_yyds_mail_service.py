from src.services.yyds_mail import YYDSMailService


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


class FakeHTTPClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({
            "method": method,
            "url": url,
            "kwargs": kwargs,
        })
        if not self.responses:
            raise AssertionError(f"未准备响应: {method} {url}")
        return self.responses.pop(0)


def test_create_email_uses_api_key_and_parses_wrapped_payload():
    service = YYDSMailService({
        "base_url": "https://maliapi.215.im/v1",
        "api_key": "AC-test-key",
        "default_domain": "public.example.com",
    })
    fake_client = FakeHTTPClient([
        FakeResponse(
            status_code=201,
            payload={
                "success": True,
                "data": {
                    "id": "inbox-1",
                    "address": "tester@public.example.com",
                    "token": "temp-token-1",
                },
            },
        ),
    ])
    service.http_client = fake_client

    email_info = service.create_email({"address": "tester"})

    assert email_info["email"] == "tester@public.example.com"
    assert email_info["service_id"] == "inbox-1"
    assert email_info["account_id"] == "inbox-1"
    assert email_info["token"] == "temp-token-1"

    create_call = fake_client.calls[0]
    assert create_call["method"] == "POST"
    assert create_call["url"] == "https://maliapi.215.im/v1/accounts"
    assert create_call["kwargs"]["headers"]["X-API-Key"] == "AC-test-key"
    assert create_call["kwargs"]["json"] == {
        "address": "tester",
        "domain": "public.example.com",
    }


def test_get_verification_code_fetches_temp_token_when_cache_missing():
    service = YYDSMailService({
        "base_url": "https://maliapi.215.im/v1",
        "api_key": "AC-test-key",
    })
    fake_client = FakeHTTPClient([
        FakeResponse(
            payload={
                "success": True,
                "data": {
                    "id": "inbox-1",
                    "address": "tester@public.example.com",
                    "token": "temp-token-1",
                },
            },
        ),
        FakeResponse(
            payload={
                "success": True,
                "data": {
                    "messages": [
                        {
                            "id": "msg-1",
                            "from": {
                                "name": "OpenAI",
                                "address": "noreply@openai.com",
                            },
                            "subject": "Your verification code",
                            "createdAt": "2026-03-27T10:00:00Z",
                        }
                    ],
                    "total": 1,
                },
            },
        ),
        FakeResponse(
            payload={
                "success": True,
                "data": {
                    "id": "msg-1",
                    "from": {
                        "name": "OpenAI",
                        "address": "noreply@openai.com",
                    },
                    "subject": "Your verification code",
                    "text": "Your OpenAI verification code is 654321",
                    "html": [],
                },
            },
        ),
    ])
    service.http_client = fake_client

    code = service.get_verification_code(
        email="tester@public.example.com",
        timeout=1,
    )

    assert code == "654321"

    token_call = fake_client.calls[0]
    assert token_call["method"] == "POST"
    assert token_call["url"] == "https://maliapi.215.im/v1/token"
    assert token_call["kwargs"]["json"] == {
        "address": "tester@public.example.com",
    }

    messages_call = fake_client.calls[1]
    assert messages_call["method"] == "GET"
    assert messages_call["url"] == "https://maliapi.215.im/v1/messages"
    assert messages_call["kwargs"]["headers"]["Authorization"] == "Bearer temp-token-1"
    assert messages_call["kwargs"]["params"]["address"] == "tester@public.example.com"

    detail_call = fake_client.calls[2]
    assert detail_call["method"] == "GET"
    assert detail_call["url"] == "https://maliapi.215.im/v1/messages/msg-1"
    assert detail_call["kwargs"]["headers"]["Authorization"] == "Bearer temp-token-1"
