from apps.api import main as api_main
from apps.worker import main as worker_main


def test_api_main_uses_uvicorn(monkeypatch):
    calls = {}

    def fake_run(target, host, port, reload):
        calls["target"] = target
        calls["host"] = host
        calls["port"] = port
        calls["reload"] = reload

    monkeypatch.setattr("apps.api.main.uvicorn.run", fake_run)

    exit_code = api_main.main(
        ["--host", "127.0.0.1", "--port", "9000", "--database-url", "sqlite:///./tmp/api.db"]
    )

    assert exit_code == 0
    assert calls["target"] == "apps.api.main:create_app"
    assert calls["host"] == "127.0.0.1"
    assert calls["port"] == 9000
    assert calls["reload"] is False


def test_worker_main_uses_run_worker_loop(monkeypatch):
    calls = {}

    def fake_run_worker_loop(database_url, max_iterations, poll_interval_seconds):
        calls["database_url"] = database_url
        calls["max_iterations"] = max_iterations
        calls["poll_interval_seconds"] = poll_interval_seconds
        return [{"success": True}]

    monkeypatch.setattr("apps.worker.main.run_worker_loop", fake_run_worker_loop)

    exit_code = worker_main.main(
        ["--database-url", "sqlite:///./tmp/worker.db", "--max-iterations", "2", "--poll-interval-seconds", "0"]
    )

    assert exit_code == 0
    assert calls["database_url"] == "sqlite:///./tmp/worker.db"
    assert calls["max_iterations"] == 2
    assert calls["poll_interval_seconds"] == 0.0
