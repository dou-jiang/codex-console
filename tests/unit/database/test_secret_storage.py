import sqlite3
from pathlib import Path

from packages.account_store.db import AccountStoreDB
from src.database.session import DatabaseSessionManager


def test_account_secret_fields_are_encrypted_at_rest(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("APP_ENCRYPTION_KEY", "unit-test-encryption-key")
    db_path = tmp_path / "secret-store.db"
    store = AccountStoreDB(database_url=f"sqlite:///{db_path}")

    store.accounts.create(
        email="tester@example.com",
        email_service="duck_mail",
        password="secret-password",
        access_token="access-123",
        refresh_token="refresh-123",
        id_token="id-123",
        session_token="session-123",
        cookies="oai-did=device-1; __Secure-next-auth.session-token=session-123",
    )

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT password, access_token, refresh_token, id_token, session_token, cookies FROM accounts WHERE email = ?",
        ("tester@example.com",),
    ).fetchone()
    conn.close()

    assert row is not None
    for item in row:
        assert isinstance(item, str)
        assert item.startswith("enc::")
        assert "secret-password" not in item
        assert "access-123" not in item


def test_plaintext_secret_fields_still_read_for_legacy_rows(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("APP_ENCRYPTION_KEY", "unit-test-encryption-key")
    db_path = tmp_path / "legacy-secret-store.db"
    manager = DatabaseSessionManager(database_url=f"sqlite:///{db_path}")
    manager.create_tables()
    manager.migrate_tables()

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO accounts (
            email, password, access_token, refresh_token, id_token, session_token,
            email_service, cookies, status, created_at, updated_at, registered_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (
            "legacy@example.com",
            "legacy-password",
            "legacy-access",
            "legacy-refresh",
            "legacy-id",
            "legacy-session",
            "manual",
            "legacy-cookie",
            "active",
        ),
    )
    conn.commit()
    conn.close()

    with manager.session_scope() as db:
        account = db.query(__import__("src.database.models", fromlist=["Account"]).Account).filter_by(email="legacy@example.com").first()
        assert account.password == "legacy-password"
        assert account.access_token == "legacy-access"
        assert account.cookies == "legacy-cookie"
