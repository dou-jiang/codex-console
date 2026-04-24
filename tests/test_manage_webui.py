from argparse import Namespace
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "manage_webui.py"
SPEC = spec_from_file_location("manage_webui", MODULE_PATH)
manage_webui = module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(manage_webui)


def test_update_repository_skips_auto_pull_when_workspace_is_dirty(monkeypatch, tmp_path):
    project_root = tmp_path / "repo"
    (project_root / ".git").mkdir(parents=True)

    warnings = []
    commands = []

    monkeypatch.setattr(manage_webui, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(manage_webui, "has_uncommitted_changes", lambda: True)
    monkeypatch.setattr(manage_webui, "warn", warnings.append)
    monkeypatch.setattr(
        manage_webui,
        "run_command",
        lambda *args, **kwargs: commands.append((args, kwargs)),
    )

    args = Namespace(skip_update=False, remote="", branch="")

    manage_webui.update_repository(args)

    assert warnings == ["Uncommitted changes detected, skip auto update for current workspace."]
    assert commands == []


def test_build_project_prefers_requirements_over_linux_build_script(monkeypatch, tmp_path):
    project_root = tmp_path / "repo"
    project_root.mkdir(parents=True)
    requirements = project_root / "requirements.txt"
    requirements.write_text("fastapi>=0.100.0\n", encoding="utf-8")
    (project_root / "build.sh").write_text("#!/bin/bash\nexit 99\n", encoding="utf-8")

    infos = []
    oks = []
    commands = []

    monkeypatch.setattr(manage_webui, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(manage_webui, "IS_WINDOWS", False)
    monkeypatch.setattr(manage_webui, "info", infos.append)
    monkeypatch.setattr(manage_webui, "ok", oks.append)
    monkeypatch.setattr(manage_webui, "run_command", lambda cmd, **kwargs: commands.append(cmd))
    monkeypatch.setattr(manage_webui, "resolve_python", lambda _python_cmd: "/usr/bin/python3")

    args = Namespace(skip_build=False, use_conda=False, python="python3")

    manage_webui.build_project(args)

    assert commands == [["/usr/bin/python3", "-m", "pip", "install", "-r", str(requirements)]]
    assert infos == ["Sync runtime dependencies from requirements.txt"]
    assert oks == ["Dependencies synchronized."]
