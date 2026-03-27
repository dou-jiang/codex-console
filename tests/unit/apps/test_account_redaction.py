import asyncio
from contextlib import contextmanager
from pathlib import Path

from packages.account_store.db import AccountStoreDB
from src.web.routes import accounts as accounts_routes


def test_account_response_redacts_password_and_cookies(tmp_path: Path):
    store = AccountStoreDB(database_url=f"sqlite:///{tmp_path / 'accounts.db'}")
    account = store.accounts.create(
        email="tester@example.com",
        email_service="duck_mail",
        password="secret-password",
        cookies="cookie-value",
    )

    payload = accounts_routes.account_to_response(account).model_dump()

    assert payload["has_password"] is True
    assert payload["has_cookies"] is True
    assert "password" not in payload
    assert "cookies" not in payload


def test_current_account_snapshot_does_not_write_raw_tokens(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = AccountStoreDB(database_url=f"sqlite:///{tmp_path / 'accounts.db'}")
    account = store.accounts.create(
        email="tester@example.com",
        email_service="duck_mail",
        access_token="access-123",
        refresh_token="refresh-123",
        id_token="id-123",
        session_token="session-123",
    )

    snapshot_path = accounts_routes._write_current_account_snapshot(account)
    content = Path(snapshot_path).read_text(encoding="utf-8")

    assert "access-123" not in content
    assert "refresh-123" not in content
    assert "session-123" not in content
    assert "\"has_access_token\": true" in content.lower()


def test_get_account_tokens_returns_preview_only(tmp_path: Path, monkeypatch):
    store = AccountStoreDB(database_url=f"sqlite:///{tmp_path / 'accounts.db'}")
    account = store.accounts.create(
        email="tester@example.com",
        email_service="duck_mail",
        access_token="access-1234567890",
        refresh_token="refresh-1234567890",
        id_token="id-1234567890",
        session_token="session-1234567890",
    )

    @contextmanager
    def fake_get_db():
        with store.manager.session_scope() as db:
            yield db

    monkeypatch.setattr(accounts_routes, "get_db", fake_get_db)

    payload = asyncio.run(accounts_routes.get_account_tokens(account.id))

    assert payload["has_access_token"] is True
    assert payload["access_token_preview"]
    assert "access_token" not in payload
    assert "refresh_token" not in payload
    assert "id_token" not in payload
    assert "session_token" not in payload
