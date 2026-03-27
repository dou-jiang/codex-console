import os
from pathlib import Path
import subprocess
import sys

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


def test_run_api_script_executes_without_pythonpath():
    repo_root = Path(__file__).resolve().parents[3]
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, "scripts/run_api.py", "--help"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Run the migrated API app." in result.stdout


def test_run_worker_script_executes_without_pythonpath():
    repo_root = Path(__file__).resolve().parents[3]
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, "scripts/run_worker.py", "--help"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Run the migrated worker loop." in result.stdout
