from pathlib import Path

from packages.account_store.db import AccountStoreDB


def test_task_store_can_record_status(tmp_path: Path):
    store = AccountStoreDB(database_url=f"sqlite:///{tmp_path / 'task-store.db'}")

    task = store.tasks.create(task_uuid="t-1", status="pending")

    assert task.task_uuid == "t-1"
    assert task.status == "pending"


def test_log_store_appends_messages(tmp_path: Path):
    store = AccountStoreDB(database_url=f"sqlite:///{tmp_path / 'log-store.db'}")
    store.tasks.create(task_uuid="t-1", status="pending")

    store.logs.append("t-1", "hello")

    assert store.logs.list("t-1") == ["hello"]


def test_log_store_appends_many_messages_in_order(tmp_path: Path):
    store = AccountStoreDB(database_url=f"sqlite:///{tmp_path / 'log-store-batch.db'}")
    store.tasks.create(task_uuid="t-1", status="pending")

    store.logs.append_many("t-1", ["line one", "line two", "line three"])

    assert store.logs.list("t-1") == ["line one", "line two", "line three"]
