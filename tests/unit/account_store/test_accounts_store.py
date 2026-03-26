from pathlib import Path

from packages.account_store.db import AccountStoreDB


def test_account_store_creates_account(tmp_path: Path):
    store = AccountStoreDB(database_url=f"sqlite:///{tmp_path / 'accounts.db'}")

    account = store.accounts.create(
        email="tester@example.com",
        email_service="duck_mail",
        password="secret",
    )

    assert account.email == "tester@example.com"
    assert account.email_service == "duck_mail"
