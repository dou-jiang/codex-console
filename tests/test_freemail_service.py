from src.services.freemail import FreemailService


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


def test_get_verification_code_filters_old_mails_by_otp_sent_at():
    service = FreemailService({
        "base_url": "https://mail.example.com",
        "admin_token": "admin-secret",
    })
    fake_client = FakeHTTPClient([
        FakeResponse(
            payload=[
                {
                    "id": "mail-old",
                    "sender": "noreply@openai.com",
                    "subject": "Old code",
                    "preview": "333333 is your verification code",
                    "created_at": 1_700_000_000 - 30,
                },
                {
                    "id": "mail-new",
                    "sender": "noreply@openai.com",
                    "subject": "New code",
                    "preview": "444444 is your verification code",
                    "created_at": 1_700_000_000 + 5,
                },
            ]
        ),
    ])
    service.http_client = fake_client

    code = service.get_verification_code(
        email="tester@example.com",
        timeout=1,
        otp_sent_at=1_700_000_000,
    )

    assert code == "444444"


def test_get_verification_code_skips_last_used_mail_between_calls():
    service = FreemailService({
        "base_url": "https://mail.example.com",
        "admin_token": "admin-secret",
    })
    fake_client = FakeHTTPClient([
        FakeResponse(
            payload=[
                {
                    "id": "mail-1",
                    "sender": "noreply@openai.com",
                    "subject": "Code #1",
                    "preview": "111111 is your verification code",
                    "created_at": 1_700_000_000,
                },
            ]
        ),
        FakeResponse(
            payload=[
                {
                    "id": "mail-1",
                    "sender": "noreply@openai.com",
                    "subject": "Code #1",
                    "preview": "111111 is your verification code",
                    "created_at": 1_700_000_000,
                },
                {
                    "id": "mail-2",
                    "sender": "noreply@openai.com",
                    "subject": "Code #2",
                    "preview": "222222 is your verification code",
                    "created_at": 1_700_000_030,
                },
            ]
        ),
    ])
    service.http_client = fake_client

    code_1 = service.get_verification_code(email="tester@example.com", timeout=1)
    code_2 = service.get_verification_code(email="tester@example.com", timeout=1)

    assert code_1 == "111111"
    assert code_2 == "222222"
