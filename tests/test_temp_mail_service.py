from src.services.temp_mail import TempMailService


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


def test_get_verification_code_fallbacks_to_admin_when_user_endpoints_fail():
    service = TempMailService({
        "base_url": "https://mail.example.com",
        "admin_password": "admin-secret",
        "domain": "example.com",
    })
    fake_client = FakeHTTPClient([
        FakeResponse(status_code=401, payload={"error": "unauthorized"}),
        FakeResponse(status_code=401, payload={"error": "unauthorized"}),
        FakeResponse(
            payload={
                "results": [
                    {
                        "id": "mail-1",
                        "source": "noreply@openai.com",
                        "subject": "OpenAI verification",
                        "text": "Your OpenAI verification code is 654321",
                    }
                ],
                "total": 1,
            }
        ),
    ])
    service.http_client = fake_client

    email = "tester@example.com"
    service._email_cache[email] = {"jwt": "jwt-abc"}

    code = service.get_verification_code(email=email, timeout=1)

    assert code == "654321"
    assert fake_client.calls[0]["url"].endswith("/api/mails")
    assert fake_client.calls[0]["kwargs"]["headers"]["Authorization"] == "Bearer jwt-abc"
    assert fake_client.calls[1]["url"].endswith("/user_api/mails")
    assert fake_client.calls[1]["kwargs"]["headers"]["x-user-token"] == "jwt-abc"
    assert fake_client.calls[2]["url"].endswith("/admin/mails")
    assert fake_client.calls[2]["kwargs"]["params"]["address"] == email


def test_get_verification_code_without_jwt_uses_admin_only():
    service = TempMailService({
        "base_url": "https://mail.example.com",
        "admin_password": "admin-secret",
        "domain": "example.com",
    })
    fake_client = FakeHTTPClient([
        FakeResponse(
            payload={
                "results": [
                    {
                        "id": "mail-1",
                        "source": "noreply@openai.com",
                        "subject": "Code",
                        "text": "123456 is your verification code",
                    }
                ],
                "total": 1,
            }
        ),
    ])
    service.http_client = fake_client

    code = service.get_verification_code(email="nojwt@example.com", timeout=1)

    assert code == "123456"
    assert len(fake_client.calls) == 1
    assert fake_client.calls[0]["url"].endswith("/admin/mails")


def test_get_verification_code_skips_last_used_mail_id_between_calls():
    service = TempMailService({
        "base_url": "https://mail.example.com",
        "admin_password": "admin-secret",
        "domain": "example.com",
    })
    fake_client = FakeHTTPClient([
        FakeResponse(
            payload={
                "results": [
                    {
                        "id": "mail-1",
                        "source": "noreply@openai.com",
                        "subject": "Code #1",
                        "text": "111111 is your verification code",
                    }
                ],
                "total": 1,
            }
        ),
        FakeResponse(
            payload={
                "results": [
                    {
                        "id": "mail-1",
                        "source": "noreply@openai.com",
                        "subject": "Code #1",
                        "text": "111111 is your verification code",
                    },
                    {
                        "id": "mail-2",
                        "source": "noreply@openai.com",
                        "subject": "Code #2",
                        "text": "222222 is your verification code",
                    },
                ],
                "total": 2,
            }
        ),
    ])
    service.http_client = fake_client

    code_1 = service.get_verification_code(email="reuse@example.com", timeout=1)
    code_2 = service.get_verification_code(email="reuse@example.com", timeout=1)

    assert code_1 == "111111"
    assert code_2 == "222222"


def test_get_verification_code_filters_old_mails_by_otp_sent_at():
    service = TempMailService({
        "base_url": "https://mail.example.com",
        "admin_password": "admin-secret",
        "domain": "example.com",
    })
    otp_sent_at = 1_700_000_000.0
    fake_client = FakeHTTPClient([
        FakeResponse(
            payload={
                "results": [
                    {
                        "id": "mail-old",
                        "source": "noreply@openai.com",
                        "subject": "Old Code",
                        "text": "333333 is your verification code",
                        "createdAt": otp_sent_at - 30,
                    },
                    {
                        "id": "mail-new",
                        "source": "noreply@openai.com",
                        "subject": "New Code",
                        "text": "444444 is your verification code",
                        "createdAt": otp_sent_at + 5,
                    },
                ],
                "total": 2,
            }
        ),
    ])
    service.http_client = fake_client

    code = service.get_verification_code(
        email="filter@example.com",
        timeout=1,
        otp_sent_at=otp_sent_at,
    )

    assert code == "444444"


def test_get_verification_code_accepts_mails_key_and_missing_mail_id():
    service = TempMailService({
        "base_url": "https://mail.example.com",
        "admin_password": "admin-secret",
        "domain": "example.com",
    })
    fake_client = FakeHTTPClient([
        FakeResponse(
            payload={
                "mails": [
                    {
                        # 没有 id/mail_id 字段，验证回退 ID 逻辑
                        "source": "noreply@openai.com",
                        "subject": "OpenAI verification",
                        "text": "Your verification code is 987654",
                        "createdAt": "2026-03-23 10:00:00",
                    }
                ],
                "total": 1,
            }
        ),
    ])
    service.http_client = fake_client

    code = service.get_verification_code(email="format@example.com", timeout=1)

    assert code == "987654"


def test_get_verification_code_fetches_mail_detail_when_list_has_no_body():
    service = TempMailService({
        "base_url": "https://mail.example.com",
        "admin_password": "admin-secret",
        "domain": "example.com",
    })
    fake_client = FakeHTTPClient([
        FakeResponse(
            payload={
                "results": [
                    {
                        "id": "mail-100",
                        "source": "noreply@openai.com",
                        "subject": "OpenAI verification",
                        "createdAt": "2026-03-23T10:00:00Z",
                    }
                ]
            }
        ),
        FakeResponse(
            payload={
                "id": "mail-100",
                "source": "noreply@openai.com",
                "subject": "OpenAI verification",
                "text": "Your OpenAI verification code is 246810",
            }
        ),
    ])
    service.http_client = fake_client

    code = service.get_verification_code(email="detail@example.com", timeout=1)

    assert code == "246810"


def test_get_verification_code_admin_unfiltered_fallback():
    service = TempMailService({
        "base_url": "https://mail.example.com",
        "admin_password": "admin-secret",
        "domain": "example.com",
    })
    fake_client = FakeHTTPClient([
        # /admin/mails?address=... 返回空
        FakeResponse(payload={"results": []}),
        # /admin/mails 不带地址过滤，返回包含目标邮箱邮件
        FakeResponse(
            payload={
                "results": [
                    {
                        "id": "mail-200",
                        "address": "target@example.com",
                        "source": "noreply@openai.com",
                        "subject": "Code",
                        "text": "135790 is your verification code",
                    },
                    {
                        "id": "mail-201",
                        "address": "other@example.com",
                        "source": "noreply@openai.com",
                        "subject": "Code",
                        "text": "111111 is your verification code",
                    },
                ]
            }
        ),
    ])
    service.http_client = fake_client

    code = service.get_verification_code(email="target@example.com", timeout=1)

    assert code == "135790"
