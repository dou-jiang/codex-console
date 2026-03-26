import asyncio

from src.web.routes.registration import run_registration_task


def test_legacy_run_registration_task_uses_new_worker_flow(monkeypatch):
    calls = {"status": [], "logs": []}

    class FakeLoop:
        def run_in_executor(self, executor, fn, *args):
            calls["executor_call"] = (fn, args)
            return {"success": True, "status": "completed", "task_uuid": "task-1"}

    class FakeSessionManager:
        database_url = "sqlite:///./tmp/legacy.db"

    monkeypatch.setattr("src.web.routes.registration.get_session_manager", lambda: FakeSessionManager())
    monkeypatch.setattr("src.web.routes.registration.task_manager.get_loop", lambda: FakeLoop())
    monkeypatch.setattr("src.web.routes.registration.task_manager.set_loop", lambda loop: None)
    monkeypatch.setattr("src.web.routes.registration.task_manager.update_status", lambda task_uuid, status, **kwargs: calls["status"].append((task_uuid, status, kwargs)))
    monkeypatch.setattr("src.web.routes.registration.task_manager.add_log", lambda task_uuid, message: calls["logs"].append((task_uuid, message)))

    asyncio.run(run_registration_task("task-1", "duck_mail", None, None))

    fn, args = calls["executor_call"]
    assert fn.__name__ == "run_task_once"
    assert args == ("sqlite:///./tmp/legacy.db", "task-1")
    assert calls["status"][0][1] == "pending"
    assert calls["status"][-1][1] == "completed"


def test_legacy_run_registration_task_reports_worker_failure(monkeypatch):
    calls = {"status": [], "logs": []}

    class FakeLoop:
        def run_in_executor(self, executor, fn, *args):
            return {"success": False, "status": "failed", "error": "boom", "task_uuid": "task-1"}

    class FakeSessionManager:
        database_url = "sqlite:///./tmp/legacy.db"

    monkeypatch.setattr("src.web.routes.registration.get_session_manager", lambda: FakeSessionManager())
    monkeypatch.setattr("src.web.routes.registration.task_manager.get_loop", lambda: FakeLoop())
    monkeypatch.setattr("src.web.routes.registration.task_manager.set_loop", lambda loop: None)
    monkeypatch.setattr("src.web.routes.registration.task_manager.update_status", lambda task_uuid, status, **kwargs: calls["status"].append((task_uuid, status, kwargs)))
    monkeypatch.setattr("src.web.routes.registration.task_manager.add_log", lambda task_uuid, message: calls["logs"].append((task_uuid, message)))

    asyncio.run(run_registration_task("task-1", "duck_mail", None, None))

    assert calls["status"][0][1] == "pending"
    assert calls["status"][-1][1] == "failed"
    assert calls["status"][-1][2]["error"] == "boom"
