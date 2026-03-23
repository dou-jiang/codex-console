import asyncio

import pytest

from src.web import task_manager as task_manager_module
from src.web.task_manager import TaskManager


class FakeWebSocket:
    def __init__(self):
        self.messages = []

    async def send_json(self, payload):
        self.messages.append(payload)


@pytest.fixture(autouse=True)
def clean_task_manager_globals():
    names = [
        "_log_queues",
        "_log_locks",
        "_ws_connections",
        "_ws_sent_index",
        "_task_status",
        "_task_steps",
        "_task_cancelled",
        "_batch_status",
        "_batch_logs",
        "_batch_locks",
    ]
    snapshots = {}
    for name in names:
        store = getattr(task_manager_module, name)
        snapshots[name] = store.copy()
        store.clear()

    yield

    for name in names:
        store = getattr(task_manager_module, name)
        store.clear()
        store.update(snapshots[name])


@pytest.mark.anyio
async def test_update_status_broadcasts_to_registered_task_websocket():
    manager = TaskManager()
    manager.set_loop(asyncio.get_running_loop())
    websocket = FakeWebSocket()
    task_uuid = "task-single-123"

    manager.register_websocket(task_uuid, websocket)
    manager.update_status(task_uuid, "completed", email="tester@example.com")
    await asyncio.sleep(0.05)

    assert manager.get_status(task_uuid)["status"] == "completed"
    assert len(websocket.messages) == 1
    message = websocket.messages[0]
    assert message["type"] == "status"
    assert message["task_uuid"] == task_uuid
    assert message["status"] == "completed"
    assert message["email"] == "tester@example.com"
    assert message["timestamp"]
