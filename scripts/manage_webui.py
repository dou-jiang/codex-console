#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIR = PROJECT_ROOT / "logs" / "runtime"
IS_WINDOWS = os.name == "nt"
DEFAULT_PYTHON = sys.executable or "python"
PUBLIC_ACTIONS = ("start", "stop", "restart", "status", "logs")
ALL_ACTIONS = PUBLIC_ACTIONS + ("watchdog",)
BOOT_TIMEOUT_SECONDS = 25


def safe_print(message: str = "", *, stream = sys.stdout) -> None:
    text = str(message)
    target = stream
    encoding = getattr(target, "encoding", None) or "utf-8"
    try:
        target.write(text + ("" if text.endswith("\n") else "\n"))
    except UnicodeEncodeError:
        sanitized = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
        target.write(sanitized + ("" if sanitized.endswith("\n") else "\n"))
    target.flush()


def banner(message: str) -> None:
    safe_print(f"\n==== {message} ====")


def step(index: int, total: int, message: str) -> None:
    safe_print(f"[{index}/{total}] {message}")


def info(message: str) -> None:
    safe_print(f"[INFO] {message}")


def ok(message: str) -> None:
    safe_print(f"[OK] {message}")


def warn(message: str) -> None:
    safe_print(f"[WARN] {message}")


def fail(message: str, code: int = 1) -> None:
    safe_print(f"[ERR] {message}", stream=sys.stderr)
    raise SystemExit(code)


def ensure_runtime_dir() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


def pid_file(port: int) -> Path:
    return RUNTIME_DIR / f"webui-{port}.pid"


def meta_file(port: int) -> Path:
    return RUNTIME_DIR / f"webui-{port}.json"


def stdout_log_file(port: int) -> Path:
    return RUNTIME_DIR / f"webui-{port}.stdout.log"


def stderr_log_file(port: int) -> Path:
    return RUNTIME_DIR / f"webui-{port}.stderr.log"


def append_runtime_log(port: int, message: str, *, error: bool = False) -> None:
    ensure_runtime_dir()
    path = stderr_log_file(port) if error else stdout_log_file(port)
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] {message}\n")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(str(temp), str(path))


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_state(port: int) -> dict:
    return read_json(meta_file(port))


def save_state(port: int, state: dict) -> None:
    atomic_write_text(meta_file(port), json.dumps(state, ensure_ascii=False, indent=2))
    primary_pid = state.get("pid")
    if primary_pid:
        atomic_write_text(pid_file(port), str(primary_pid))


def update_state(port: int, **fields) -> dict:
    state = load_state(port)
    state.update(fields)
    save_state(port, state)
    return state


def cleanup_state(port: int) -> None:
    for path in (pid_file(port), meta_file(port)):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def cleanup_runtime_artifacts(port: int) -> None:
    cleanup_state(port)
    for path in (stdout_log_file(port), stderr_log_file(port)):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def resolve_python(python_cmd: str) -> str:
    if Path(python_cmd).exists():
        return str(Path(python_cmd).resolve())
    found = shutil.which(python_cmd)
    if found:
        return found
    fail(f"Python executable not found: {python_cmd}")
    raise AssertionError


def run_command(
    cmd: Iterable[str],
    *,
    cwd: Path = PROJECT_ROOT,
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    kwargs = {
        "cwd": str(cwd),
        "text": True,
        "check": False,
    }
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    result = subprocess.run(list(cmd), **kwargs)
    if check and result.returncode != 0:
        output = ""
        if capture:
            output = f"\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        fail(f"Command failed ({result.returncode}): {' '.join(cmd)}{output}")
    return result


def local_access_host(host: str) -> str:
    return "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host


def port_probe_host(host: str) -> str:
    return local_access_host(host)


def format_url_template(template: str, host: str, port: int) -> str:
    return template.format(host=local_access_host(host), port=port)


def build_browser_url(host: str, port: int) -> str:
    return f"http://{local_access_host(host)}:{port}"


def build_health_url(host: str, port: int, health_url: str) -> str:
    template = health_url or "http://{host}:{port}/login"
    return format_url_template(template, host, port)


def runtime_mode_name(args: argparse.Namespace) -> str:
    if args.use_conda:
        return f"conda:{get_conda_env_name(args)}"
    return resolve_python(args.python)


def find_conda_command() -> Optional[list[str]]:
    candidates = ["conda.exe", "conda", "conda.bat", "conda.cmd"] if IS_WINDOWS else ["conda"]
    for name in candidates:
        found = shutil.which(name)
        if not found:
            continue
        suffix = Path(found).suffix.lower()
        if IS_WINDOWS and suffix in {".bat", ".cmd"}:
            comspec = os.environ.get("COMSPEC", "cmd.exe")
            return [comspec, "/c", found]
        return [found]
    return None


def get_conda_env_name(args: argparse.Namespace) -> str:
    value = str(args.conda_env or "").strip()
    return value or PROJECT_ROOT.name


def get_conda_env_catalog(conda_cmd: list[str]) -> dict[str, Path]:
    result = run_command(conda_cmd + ["env", "list", "--json"], capture=True, check=False)
    if result.returncode != 0:
        fail("Conda is installed but failed to list environments. Please check your conda installation.")
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        fail(f"Failed to parse conda env list output: {exc}")

    catalog: dict[str, Path] = {}

    def register(key: str, value: str) -> None:
        key = str(key).strip()
        value = str(value).strip()
        if not key or not value:
            return
        catalog[key] = Path(value)

    for env_path in payload.get("envs", []):
        text = str(env_path).strip()
        if not text:
            continue
        register(text, text)
        register(Path(text).name, text)

    for env_path, details in (payload.get("envs_details") or {}).items():
        text = str(env_path).strip()
        if text:
            register(text, text)
            register(Path(text).name, text)
        if isinstance(details, dict):
            name = str(details.get("name") or "").strip()
            if name:
                register(name, text)

    return catalog


def ensure_conda_ready(args: argparse.Namespace) -> tuple[list[str], str, Path]:
    conda_cmd = find_conda_command()
    if not conda_cmd:
        fail("Conda was not found. Please install Miniconda/Anaconda first, then retry with --use-conda.")
    env_name = get_conda_env_name(args)
    catalog = get_conda_env_catalog(conda_cmd)
    env_path = catalog.get(env_name)
    if env_path is None:
        fail(
            f"Conda environment '{env_name}' was not found. "
            f"Create it first or specify another one with --conda-env."
        )
    return conda_cmd, env_name, env_path


def get_conda_env_python(args: argparse.Namespace) -> str:
    _conda_cmd, env_name, env_path = ensure_conda_ready(args)
    candidate = env_path / ("python.exe" if IS_WINDOWS else "bin/python")
    if not candidate.exists():
        fail(f"Python executable was not found in conda environment '{env_name}': {candidate}")
    return str(candidate)


def resolve_runtime_python(args: argparse.Namespace) -> str:
    if args.use_conda:
        return get_conda_env_python(args)
    return resolve_python(args.python)


def build_python_prefix(args: argparse.Namespace) -> list[str]:
    return [resolve_runtime_python(args)]


def build_webui_command(args: argparse.Namespace) -> list[str]:
    webui_script = PROJECT_ROOT / "webui.py"
    if not webui_script.exists():
        fail(f"Startup entry not found: {webui_script}")
    cmd = build_python_prefix(args) + [str(webui_script), "--host", args.host, "--port", str(args.port)]
    if args.debug:
        cmd.append("--debug")
    return cmd


def has_uncommitted_changes() -> bool:
    result = run_command(["git", "status", "--porcelain"], capture=True)
    return bool((result.stdout or "").strip())


def get_git_upstream() -> str:
    result = run_command(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        capture=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def get_current_branch() -> str:
    result = run_command(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture=True)
    return (result.stdout or "").strip()


def get_remotes() -> list[str]:
    result = run_command(["git", "remote"], capture=True)
    return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]


def update_repository(args: argparse.Namespace) -> None:
    if args.skip_update:
        warn("Skip git update by flag.")
        return

    if not (PROJECT_ROOT / ".git").exists():
        warn("Current project is not a git repository, skip update.")
        return

    if has_uncommitted_changes():
        warn("Uncommitted changes detected, skip auto update for current workspace.")
        return

    if bool(args.remote) ^ bool(args.branch):
        fail("--remote and --branch must be used together.")

    if args.remote and args.branch:
        info(f"Fetch and fast-forward from {args.remote}/{args.branch}")
        run_command(["git", "fetch", args.remote, args.branch, "--progress"])
        run_command(["git", "merge", "--ff-only", "FETCH_HEAD"])
        ok(f"Repository updated to {args.remote}/{args.branch}")
        return

    upstream = get_git_upstream()
    if upstream:
        info(f"Pull latest code from upstream {upstream}")
        run_command(["git", "pull", "--ff-only"])
        ok(f"Repository updated from {upstream}")
        return

    branch = get_current_branch()
    remotes = get_remotes()
    if "origin" in remotes and branch:
        info(f"No upstream configured, fallback to origin/{branch}")
        run_command(["git", "fetch", "origin", branch, "--progress"])
        run_command(["git", "merge", "--ff-only", f"origin/{branch}"])
        ok(f"Repository updated from origin/{branch}")
        return

    warn("No upstream/remote branch available, skip auto update.")


def build_project(args: argparse.Namespace) -> None:
    requirements = PROJECT_ROOT / "requirements.txt"

    if args.skip_build:
        warn("Skip dependency sync by flag.")
        return

    if args.use_conda:
        conda_cmd, env_name, _env_path = ensure_conda_ready(args)
        env_python = get_conda_env_python(args)
        info(f"Use conda environment for build/runtime: {env_name}")
        if requirements.exists():
            info("Sync runtime dependencies in conda environment from requirements.txt")
            run_command([env_python, "-m", "pip", "install", "-r", str(requirements)])
            ok("Dependencies synchronized in conda environment.")
            return
        if IS_WINDOWS and (PROJECT_ROOT / "build.bat").exists():
            warn("requirements.txt not found, fallback to Windows build script inside conda environment")
            run_command(conda_cmd + ["run", "--no-capture-output", "-n", env_name, "cmd", "/c", "build.bat"])
            ok("Windows build completed in conda environment.")
            return
        if (not IS_WINDOWS) and (PROJECT_ROOT / "build.sh").exists():
            bash_bin = shutil.which("bash") or shutil.which("sh")
            if not bash_bin:
                fail("bash/sh not found, cannot run build.sh")
            warn("requirements.txt not found, fallback to Linux build script inside conda environment")
            run_command(conda_cmd + ["run", "--no-capture-output", "-n", env_name, bash_bin, "./build.sh"])
            ok("Linux build completed in conda environment.")
            return
        warn("No build script or requirements.txt found, skip build.")
        return

    if requirements.exists():
        info("Sync runtime dependencies from requirements.txt")
        run_command([resolve_python(args.python), "-m", "pip", "install", "-r", str(requirements)])
        ok("Dependencies synchronized.")
        return

    if IS_WINDOWS and (PROJECT_ROOT / "build.bat").exists():
        warn("requirements.txt not found, fallback to Windows build script: build.bat")
        run_command(["cmd", "/c", "build.bat"])
        ok("Windows build completed.")
        return

    if (not IS_WINDOWS) and (PROJECT_ROOT / "build.sh").exists():
        bash_bin = shutil.which("bash") or shutil.which("sh")
        if not bash_bin:
            fail("bash/sh not found, cannot run build.sh")
        warn("requirements.txt not found, fallback to Linux build script: build.sh")
        run_command([bash_bin, "./build.sh"])
        ok("Linux build completed.")
        return

    warn("No build script or requirements.txt found, skip build.")


def verify_runtime_dependencies(args: argparse.Namespace) -> None:
    runtime_python = resolve_runtime_python(args)
    probe = (
        "import sqlalchemy, fastapi, uvicorn, jinja2, multipart, aiosqlite, "
        "pydantic_settings, curl_cffi, websockets"
    )
    result = run_command([runtime_python, "-c", probe], capture=True, check=False)
    if result.returncode == 0:
        ok("Runtime dependency check passed.")
        return

    details = (result.stderr or result.stdout or "").strip()
    if args.skip_build:
        fail(
            "Runtime dependencies are missing in the selected environment. "
            "Please rerun without --skip-build, or manually execute: "
            f"{runtime_python} -m pip install -r requirements.txt\n{details}"
        )

    fail(
        "Runtime dependency check still failed after build/install. "
        "Please inspect the environment manually.\n"
        f"{details}"
    )


def can_bind_port(host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((port_probe_host(host), port))
        return True
    except OSError:
        return False


def get_port_pid(port: int) -> Optional[int]:
    if IS_WINDOWS:
        result = run_command(["netstat", "-ano", "-p", "TCP"], capture=True, check=False)
        for line in (result.stdout or "").splitlines():
            if f":{port}" not in line or "LISTENING" not in line.upper():
                continue
            parts = line.split()
            if len(parts) >= 5:
                try:
                    return int(parts[-1])
                except ValueError:
                    continue
        return None

    candidates = [
        ["lsof", f"-tiTCP:{port}", "-sTCP:LISTEN"],
        ["ss", "-ltnp"],
        ["netstat", "-ltnp"],
    ]
    for cmd in candidates:
        if shutil.which(cmd[0]) is None:
            continue
        result = run_command(cmd, capture=True, check=False)
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        if cmd[0] == "lsof":
            line = next((x.strip() for x in output.splitlines() if x.strip()), "")
            if line.isdigit():
                return int(line)
            continue
        match = re.search(rf":{port}\b.*?pid=(\d+)", output)
        if match:
            return int(match.group(1))
        match = re.search(rf":{port}\b.*?\s(\d+)/", output)
        if match:
            return int(match.group(1))
    return None


def suggest_port(host: str, start_port: int) -> Optional[int]:
    for candidate in range(start_port + 1, start_port + 51):
        if can_bind_port(host, candidate):
            return candidate
    return None


def choose_available_port(args: argparse.Namespace) -> int:
    requested_port = int(args.port)
    managed_pid = read_pid(requested_port)
    if managed_pid and is_process_alive(managed_pid):
        fail(f"Port {requested_port} is already managed by PID {managed_pid}, run stop/restart first.")
    if managed_pid and not is_process_alive(managed_pid):
        cleanup_runtime_artifacts(requested_port)

    occupied_pid = get_port_pid(requested_port)
    if occupied_pid is None or can_bind_port(args.host, requested_port):
        return requested_port

    suggestion = suggest_port(args.host, requested_port)
    if suggestion is None:
        fail(f"Port {requested_port} is occupied by PID {occupied_pid}. No spare port found in +50 range.")

    warn(
        f"Port {requested_port} is occupied by PID {occupied_pid}; "
        f"automatically switching to available port {suggestion}."
    )
    args.port = suggestion
    return suggestion


def read_pid(port: int) -> Optional[int]:
    path = pid_file(port)
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def first_live_pid(*candidates: object) -> Optional[int]:
    for item in candidates:
        try:
            pid = int(item)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if pid > 0 and is_process_alive(pid):
            return pid
    return None


def terminate_pid_tree(pid: int) -> None:
    if not is_process_alive(pid):
        return

    if IS_WINDOWS:
        run_command(["taskkill", "/PID", str(pid), "/T", "/F"], check=False)
        return

    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return

    deadline = time.time() + 8
    while time.time() < deadline:
        if not is_process_alive(pid):
            return
        time.sleep(0.4)

    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def build_state(args: argparse.Namespace, *, mode: str, primary_pid: int, webui_pid: Optional[int], guard_pid: Optional[int]) -> dict:
    return {
        "mode": mode,
        "pid": primary_pid,
        "webui_pid": webui_pid,
        "guard_pid": guard_pid,
        "port": args.port,
        "host": args.host,
        "debug": bool(args.debug),
        "python": resolve_runtime_python(args),
        "runtime_mode": "conda" if args.use_conda else "python",
        "conda_env": get_conda_env_name(args) if args.use_conda else "",
        "stdout_log": str(stdout_log_file(args.port)),
        "stderr_log": str(stderr_log_file(args.port)),
        "health_url": build_health_url(args.host, args.port, args.health_url),
        "browser_url": build_browser_url(args.host, args.port),
        "guard_enabled": bool(args.guard),
        "health_interval": float(args.health_interval),
        "health_timeout": float(args.health_timeout),
        "health_fail_threshold": int(args.health_fail_threshold),
        "restart_count": 0,
        "status": "starting",
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "last_restart_at": None,
        "last_health_check_at": None,
        "last_health_ok_at": None,
        "last_health_result": "unknown",
        "last_error": "",
    }


def probe_health(url: str, timeout_seconds: float) -> tuple[bool, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "codex-console-manager"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            code = response.getcode() or 200
            return code < 500, f"HTTP {code}"
    except urllib.error.HTTPError as exc:
        if exc.code < 500:
            return True, f"HTTP {exc.code}"
        return False, f"HTTP {exc.code}"
    except Exception as exc:
        return False, str(exc)


def wait_for_service(args: argparse.Namespace, *, watched_pid: Optional[int], timeout_seconds: int = BOOT_TIMEOUT_SECONDS) -> tuple[bool, str]:
    health_url = build_health_url(args.host, args.port, args.health_url)
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        time.sleep(1)
        if watched_pid is not None and not is_process_alive(watched_pid):
            return False, f"process {watched_pid} exited"
        healthy, detail = probe_health(health_url, float(args.health_timeout))
        if healthy:
            return True, detail
    return False, "health probe timeout"


def print_recent_logs(port: int, tail_lines: int = 20) -> None:
    for path in (stderr_log_file(port), stdout_log_file(port)):
        if not path.exists():
            continue
        print(f"--- recent log: {path} ---", flush=True)
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        for line in lines[-tail_lines:]:
            print(line, flush=True)


def open_browser(url: str) -> None:
    try:
        if IS_WINDOWS:
            os.startfile(url)  # type: ignore[attr-defined]
            return
        if sys.platform == "darwin":
            subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        opener = shutil.which("xdg-open")
        if opener:
            subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        warn(f"Browser opener not found, open manually: {url}")
    except Exception as exc:
        warn(f"Failed to open browser automatically: {exc}")


def clear_runtime_logs(port: int) -> None:
    for path in (stdout_log_file(port), stderr_log_file(port)):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def spawn_webui_process(args: argparse.Namespace, *, detached: bool) -> subprocess.Popen:
    cmd = build_webui_command(args)
    stdout_handle = stdout_log_file(args.port).open("a", encoding="utf-8")
    stderr_handle = stderr_log_file(args.port).open("a", encoding="utf-8")
    try:
        kwargs = {
            "cwd": str(PROJECT_ROOT),
            "stdout": stdout_handle,
            "stderr": stderr_handle,
            "stdin": subprocess.DEVNULL,
        }
        if IS_WINDOWS:
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            if detached:
                creationflags |= subprocess.DETACHED_PROCESS
            process = subprocess.Popen(cmd, creationflags=creationflags, **kwargs)
        else:
            process = subprocess.Popen(cmd, start_new_session=detached, **kwargs)
    finally:
        stdout_handle.close()
        stderr_handle.close()
    return process


def start_direct(args: argparse.Namespace) -> None:
    process = spawn_webui_process(args, detached=True)
    state = build_state(args, mode="direct", primary_pid=process.pid, webui_pid=process.pid, guard_pid=None)
    save_state(args.port, state)

    ready, detail = wait_for_service(args, watched_pid=process.pid)
    if not ready:
        print_recent_logs(args.port)
        fail(f"WebUI failed to start: {detail}")

    actual_webui_pid = get_port_pid(args.port) or process.pid
    state = load_state(args.port)
    state["status"] = "running"
    state["webui_pid"] = actual_webui_pid
    state["last_health_result"] = detail
    state["last_health_ok_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    state["last_health_check_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(args.port, state)

    ok(f"Service started successfully, PID={process.pid}, port={args.port}")
    info(f"runtime: {runtime_mode_name(args)}")
    info(f"stdout log: {stdout_log_file(args.port)}")
    info(f"stderr log: {stderr_log_file(args.port)}")
    info(f"visit: {build_browser_url(args.host, args.port)}")
    if args.open_browser:
        open_browser(build_browser_url(args.host, args.port))


def start_guarded(args: argparse.Namespace) -> None:
    python_bin = resolve_python(args.python)
    driver = Path(__file__).resolve()
    cmd = [
        python_bin,
        str(driver),
        "watchdog",
        "--port",
        str(args.port),
        "--host",
        args.host,
        "--python",
        python_bin,
        "--health-url",
        args.health_url,
        "--health-interval",
        str(args.health_interval),
        "--health-timeout",
        str(args.health_timeout),
        "--health-fail-threshold",
        str(args.health_fail_threshold),
    ]
    if args.debug:
        cmd.append("--debug")
    if args.use_conda:
        cmd.append("--use-conda")
        cmd += ["--conda-env", get_conda_env_name(args)]

    stdout_handle = stdout_log_file(args.port).open("a", encoding="utf-8")
    stderr_handle = stderr_log_file(args.port).open("a", encoding="utf-8")
    try:
        kwargs = {
            "cwd": str(PROJECT_ROOT),
            "stdout": stdout_handle,
            "stderr": stderr_handle,
            "stdin": subprocess.DEVNULL,
        }
        if IS_WINDOWS:
            process = subprocess.Popen(
                cmd,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
                **kwargs,
            )
        else:
            process = subprocess.Popen(cmd, start_new_session=True, **kwargs)
    finally:
        stdout_handle.close()
        stderr_handle.close()

    state = build_state(args, mode="guarded", primary_pid=process.pid, webui_pid=None, guard_pid=process.pid)
    state["guard_enabled"] = True
    state["status"] = "watchdog-starting"
    save_state(args.port, state)

    ready, detail = wait_for_service(args, watched_pid=process.pid)
    if not ready:
        print_recent_logs(args.port)
        fail(f"Guarded WebUI failed to start: {detail}")

    state = load_state(args.port)
    state["status"] = "running"
    state["webui_pid"] = get_port_pid(args.port)
    state["last_health_result"] = detail
    state["last_health_ok_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    state["last_health_check_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(args.port, state)

    ok(f"Guard mode enabled, watchdog PID={process.pid}, port={args.port}")
    info(f"runtime: {runtime_mode_name(args)}")
    info(f"stdout log: {stdout_log_file(args.port)}")
    info(f"stderr log: {stderr_log_file(args.port)}")
    info(f"visit: {build_browser_url(args.host, args.port)}")
    if args.open_browser:
        open_browser(build_browser_url(args.host, args.port))


def stop_process(port: int) -> None:
    ensure_runtime_dir()
    state = load_state(port)
    pid = first_live_pid(read_pid(port), state.get("pid"), state.get("guard_pid"), state.get("webui_pid"))
    if not pid:
        warn(f"PID file not found: {pid_file(port)}")
        cleanup_state(port)
        return

    info(f"Stopping managed process tree PID={pid}")
    terminate_pid_tree(pid)

    deadline = time.time() + 10
    while time.time() < deadline:
        if not is_process_alive(pid):
            break
        time.sleep(0.5)

    if is_process_alive(pid):
        fail(f"Failed to stop PID {pid}")

    cleanup_state(port)
    ok(f"Managed instance on port {port} stopped.")


def status_process(port: int) -> None:
    state = load_state(port)
    pid = read_pid(port) or state.get("pid")
    webui_pid = state.get("webui_pid")
    guard_pid = state.get("guard_pid")
    host = state.get("host") or "127.0.0.1"
    health_url = state.get("health_url") or build_health_url(host, port, "")
    listening_pid = get_port_pid(port)

    primary_alive = bool(pid and is_process_alive(int(pid)))
    webui_alive = bool(webui_pid and is_process_alive(int(webui_pid)))
    healthy = False
    health_detail = "not checked"
    if listening_pid is not None:
        healthy, health_detail = probe_health(health_url, float(state.get("health_timeout") or 5))

    if primary_alive and listening_pid is not None and healthy:
        overall = "running"
    elif primary_alive and listening_pid is not None:
        overall = "degraded"
    elif primary_alive:
        overall = "starting"
    elif listening_pid is not None:
        overall = "orphan-listener"
    else:
        overall = "stopped"

    banner(f"codex-console status (port={port})")
    info(f"overall status: {overall}")
    info(f"mode: {state.get('mode', 'unknown')}")
    info(f"runtime mode: {state.get('runtime_mode', 'python')}")
    info(f"conda env: {state.get('conda_env') or '-'}")
    info(f"primary pid: {pid or '-'} (alive={'yes' if primary_alive else 'no'})")
    info(f"guard pid: {guard_pid or '-'}")
    info(f"webui pid: {webui_pid or '-'} (alive={'yes' if webui_alive else 'no'})")
    info(f"listening pid: {listening_pid or '-'}")
    info(f"health url: {health_url}")
    info(f"health result: {health_detail}")
    info(f"browser url: {state.get('browser_url') or build_browser_url(host, port)}")
    info(f"restart count: {state.get('restart_count', 0)}")
    info(f"started at: {state.get('started_at', '-')}")
    info(f"last restart at: {state.get('last_restart_at', '-')}")
    info(f"last health ok at: {state.get('last_health_ok_at', '-')}")
    info(f"stdout log: {state.get('stdout_log') or stdout_log_file(port)}")
    info(f"stderr log: {state.get('stderr_log') or stderr_log_file(port)}")
    if state.get("last_error"):
        warn(f"last error: {state.get('last_error')}")


def show_logs(port: int, lines: int) -> None:
    banner(f"codex-console logs (port={port})")
    for path in (stdout_log_file(port), stderr_log_file(port)):
        safe_print(f"--- {path} ---")
        if not path.exists():
            safe_print("missing")
            continue
        content = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if not content:
            safe_print("(empty)")
            continue
        for line in content[-lines:]:
            safe_print(line)


def run_watchdog(args: argparse.Namespace) -> None:
    ensure_runtime_dir()
    append_runtime_log(args.port, f"[GUARD] Watchdog started on port {args.port}")

    stop_requested = False
    child: Optional[subprocess.Popen] = None
    restart_count = int(load_state(args.port).get("restart_count") or 0)

    def handle_stop(_signum, _frame) -> None:
        nonlocal stop_requested
        stop_requested = True
        append_runtime_log(args.port, "[GUARD] Stop signal received")

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)

    while not stop_requested:
        if child is None or child.poll() is not None:
            if child is not None:
                restart_count += 1
                append_runtime_log(args.port, f"[GUARD] Detected webui exit, restarting (count={restart_count})", error=True)
            else:
                append_runtime_log(args.port, "[GUARD] Launching webui child")

            child = spawn_webui_process(args, detached=False)
            state = load_state(args.port)
            state.update(
                {
                    "mode": "guarded",
                    "pid": os.getpid(),
                    "guard_pid": os.getpid(),
                    "webui_pid": child.pid,
                    "guard_enabled": True,
                    "status": "running",
                    "restart_count": restart_count,
                    "last_restart_at": time.strftime("%Y-%m-%d %H:%M:%S") if restart_count else state.get("last_restart_at"),
                    "last_error": "",
                }
            )
            save_state(args.port, state)

            boot_ok, boot_detail = wait_for_service(args, watched_pid=child.pid)
            state = load_state(args.port)
            state["last_health_check_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            state["last_health_result"] = boot_detail
            state["webui_pid"] = get_port_pid(args.port) or child.pid
            if boot_ok:
                state["last_health_ok_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                save_state(args.port, state)
                append_runtime_log(args.port, f"[GUARD] Child webui ready: {boot_detail}")
            else:
                state["status"] = "degraded"
                state["last_error"] = boot_detail
                save_state(args.port, state)
                append_runtime_log(args.port, f"[GUARD] Child boot health failed: {boot_detail}", error=True)
                terminate_pid_tree(child.pid)
                child = None
                time.sleep(2)
                continue

        health_failures = 0
        while child is not None and child.poll() is None and not stop_requested:
            time.sleep(float(args.health_interval))
            health_url = build_health_url(args.host, args.port, args.health_url)
            healthy, detail = probe_health(health_url, float(args.health_timeout))
            state = load_state(args.port)
            state["last_health_check_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            state["last_health_result"] = detail
            state["webui_pid"] = get_port_pid(args.port) or state.get("webui_pid")

            if healthy:
                health_failures = 0
                state["status"] = "running"
                state["last_health_ok_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                state["last_error"] = ""
                save_state(args.port, state)
                continue

            health_failures += 1
            state["status"] = "degraded"
            state["last_error"] = detail
            save_state(args.port, state)
            append_runtime_log(
                args.port,
                f"[GUARD] Health check failed ({health_failures}/{args.health_fail_threshold}): {detail}",
                error=True,
            )
            if health_failures >= int(args.health_fail_threshold):
                append_runtime_log(args.port, "[GUARD] Failure threshold reached, restarting child", error=True)
                terminate_pid_tree(child.pid)
                child = None
                break

    if child is not None and child.poll() is None:
        terminate_pid_tree(child.pid)
    append_runtime_log(args.port, "[GUARD] Watchdog stopped")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage codex-console webui")
    parser.add_argument("action", nargs="?", default="start", choices=ALL_ACTIONS)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--skip-update", action="store_true")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--open-browser", action="store_true")
    parser.add_argument("--guard", action="store_true")
    parser.add_argument("--use-conda", action="store_true")
    parser.add_argument("--conda-env", default="")
    parser.add_argument("--health-url", default="")
    parser.add_argument("--health-interval", type=float, default=15.0)
    parser.add_argument("--health-timeout", type=float, default=5.0)
    parser.add_argument("--health-fail-threshold", type=int, default=3)
    parser.add_argument("--remote", default="")
    parser.add_argument("--branch", default="")
    parser.add_argument("--python", default=DEFAULT_PYTHON)
    parser.add_argument("--lines", type=int, default=80)
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.action == "watchdog":
        run_watchdog(args)
        return

    banner(f"codex-console manager ({args.action})")
    info(f"project root: {PROJECT_ROOT}")
    info(
        "host={host} port={port} debug={debug} guard={guard} runtime={runtime}".format(
            host=args.host,
            port=args.port,
            debug="on" if args.debug else "off",
            guard="on" if args.guard else "off",
            runtime=(f"conda:{get_conda_env_name(args)}" if args.use_conda else "python"),
        )
    )

    if args.action == "status":
        status_process(args.port)
        return

    if args.action == "logs":
        show_logs(args.port, max(1, int(args.lines)))
        return

    if args.action == "stop":
        step(1, 2, "Stopping managed instance")
        stop_process(args.port)
        step(2, 2, "Stop workflow completed")
        ok("stop done")
        return

    if args.action == "restart":
        step(1, 5, "Stopping old instance")
        stop_process(args.port)
        step(2, 5, "Checking port availability")
        choose_available_port(args)
        step(3, 5, "Updating repository")
        update_repository(args)
        step(4, 6, "Syncing runtime dependencies")
        build_project(args)
        step(5, 6, "Verifying runtime dependencies")
        verify_runtime_dependencies(args)
        clear_runtime_logs(args.port)
        step(6, 6, "Launching service")
        if args.guard:
            start_guarded(args)
        else:
            start_direct(args)
        ok("restart done")
        return

    step(1, 6, "Checking runtime directory")
    ensure_runtime_dir()
    step(2, 6, "Checking port availability")
    choose_available_port(args)
    step(3, 6, "Updating repository")
    update_repository(args)
    step(4, 6, "Syncing runtime dependencies")
    build_project(args)
    step(5, 6, "Verifying runtime dependencies")
    verify_runtime_dependencies(args)
    clear_runtime_logs(args.port)
    step(6, 6, "Launching service")
    if args.guard:
        start_guarded(args)
    else:
        start_direct(args)
    ok("start done")


if __name__ == "__main__":
    main()
