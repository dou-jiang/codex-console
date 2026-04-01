import base64
import json
from datetime import datetime

from src.core.upload.cpa_upload import generate_token_json
from src.database.models import Account


def _jwt_with_payload(payload: dict) -> str:
    header = {"alg": "none", "typ": "JWT"}

    def _b64(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{_b64(header)}.{_b64(payload)}."


def test_generate_token_json_backfills_time_and_account_id_from_access_token():
    payload = {
        "iat": 1775019821,  # 2026-04-01T05:03:41Z
        "exp": 1775883821,  # 2026-04-11T05:03:41Z
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acct_from_jwt",
        },
        "https://api.openai.com/profile": {
            "email": "jwt@example.com",
        },
    }

    account = Account(
        email="ericvillegas9964@outlook.com",
        email_service="luckmail",
        access_token=_jwt_with_payload(payload),
        account_id="",
        last_refresh=None,
        expires_at=None,
    )

    token_data = generate_token_json(account)

    assert token_data["account_id"] == "acct_from_jwt"
    assert token_data["email"] == "ericvillegas9964@outlook.com"
    assert token_data["last_refresh"] == "2026-04-01T13:03:41+08:00"
    assert token_data["expired"] == "2026-04-11T13:03:41+08:00"
    assert token_data["id_token"] == account.access_token
    assert token_data["workspace_id"] == "acct_from_jwt"
    assert token_data["chatgpt_account_id"] == "acct_from_jwt"


def test_generate_token_json_prefers_db_timestamps_over_jwt_claims():
    payload = {
        "iat": 1775019821,
        "exp": 1775883821,
        "https://api.openai.com/auth": {"chatgpt_account_id": "acct_from_jwt"},
    }

    account = Account(
        email="db@example.com",
        email_service="luckmail",
        access_token=_jwt_with_payload(payload),
        account_id="acct_from_db",
        last_refresh=datetime(2026, 4, 2, 0, 0, 0),  # naive UTC
        expires_at=datetime(2026, 4, 3, 0, 0, 0),  # naive UTC
    )

    token_data = generate_token_json(account)

    assert token_data["account_id"] == "acct_from_db"
    assert token_data["last_refresh"] == "2026-04-02T08:00:00+08:00"
    assert token_data["expired"] == "2026-04-03T08:00:00+08:00"
    assert token_data["chatgpt_account_id"] == "acct_from_db"


def test_generate_token_json_uses_metadata_expires_when_db_and_jwt_missing():
    account = Account(
        email="meta@example.com",
        email_service="luckmail",
        access_token="",
        account_id="acct_meta",
        last_refresh=None,
        expires_at=None,
        extra_data={"expires": "2026-06-30T05:03:46.241Z"},
    )

    token_data = generate_token_json(account)

    assert token_data["expired"] == "2026-06-30T13:03:46+08:00"
    assert token_data["id_token"] == ""
