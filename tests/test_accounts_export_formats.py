from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from src.web.routes import accounts as accounts_routes


ROOT = Path(__file__).resolve().parents[1]


def test_build_cockpit_account_export_returns_cockpit_compatible_shape():
    created_at = datetime(2026, 1, 2, 3, 4, 5)
    last_used_at = datetime(2026, 1, 3, 4, 5, 6, tzinfo=timezone.utc)
    account = SimpleNamespace(
        id=12,
        email="demo@example.com",
        access_token="access.jwt.token",
        refresh_token="rt_123",
        id_token="id.jwt.token",
        account_id="acc_1",
        workspace_id="org_1",
        subscription_type="team",
        created_at=created_at,
        registered_at=None,
        last_used_at=last_used_at,
        last_refresh=None,
        updated_at=None,
        extra_data={"tags": ["team", "import"]},
    )

    payload = accounts_routes._build_cockpit_account_export(account)

    assert payload["id"] == "acc_1"
    assert payload["email"] == "demo@example.com"
    assert payload["auth_mode"] == "oauth"
    assert payload["plan_type"] == "Team"
    assert payload["account_id"] == "acc_1"
    assert payload["organization_id"] == "org_1"
    assert payload["tokens"] == {
        "id_token": "id.jwt.token",
        "access_token": "access.jwt.token",
        "refresh_token": "rt_123",
    }
    assert payload["id_token"] == "id.jwt.token"
    assert payload["access_token"] == "access.jwt.token"
    assert payload["refresh_token"] == "rt_123"
    assert payload["tags"] == ["team", "import"]
    assert payload["created_at"] == int(created_at.replace(tzinfo=timezone.utc).timestamp())
    assert payload["last_used"] == int(last_used_at.timestamp())


def test_accounts_page_contains_cockpit_export_entry():
    content = (ROOT / "templates" / "accounts.html").read_text(encoding="utf-8")
    assert 'data-format="cockpit"' in content


def test_build_cockpit_tokens_generates_synthetic_id_token_when_missing():
    account = SimpleNamespace(
        id=3,
        email="fallback@example.com",
        access_token="header.payload.signature",
        refresh_token="",
        id_token="",
        account_id="acc_fallback",
        workspace_id="org_fallback",
        subscription_type=None,
        created_at=None,
        registered_at=None,
        last_used_at=None,
        last_refresh=None,
        updated_at=None,
        extra_data=None,
    )

    tokens = accounts_routes._build_cockpit_tokens(account)

    assert tokens["access_token"] == "header.payload.signature"
    assert tokens["id_token"].count(".") == 2
    assert tokens["id_token"] != "header.payload.signature"
