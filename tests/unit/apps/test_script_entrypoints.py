from scripts import run_api, run_worker


def test_run_api_script_delegates(monkeypatch):
    calls = {}

    def fake_main(argv=None):
        calls["argv"] = argv
        return 0

    monkeypatch.setattr("scripts.run_api.api_main", fake_main)

    exit_code = run_api.main(["--port", "9000"])

    assert exit_code == 0
    assert calls["argv"] == ["--port", "9000"]


def test_run_worker_script_delegates(monkeypatch):
    calls = {}

    def fake_main(argv=None):
        calls["argv"] = argv
        return 0

    monkeypatch.setattr("scripts.run_worker.worker_main", fake_main)

    exit_code = run_worker.main(["--max-iterations", "2"])

    assert exit_code == 0
    assert calls["argv"] == ["--max-iterations", "2"]
