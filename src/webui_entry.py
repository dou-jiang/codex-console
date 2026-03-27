"""Packaged Web UI entrypoint with startup safety checks."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import sys

import uvicorn

from src.config.project_notice import build_terminal_notice_lines
from src.config.settings import get_settings, update_settings
from src.core.db_logs import install_database_log_handler
from src.core.timezone_utils import apply_process_timezone
from src.core.utils import setup_logging
from src.database.init_db import initialize_database


PROJECT_ROOT = Path(__file__).resolve().parent.parent

LEGACY_ENV_ALIASES = {
    "WEBUI_HOST": "APP_HOST",
    "WEBUI_PORT": "APP_PORT",
    "WEBUI_ACCESS_PASSWORD": "APP_ACCESS_PASSWORD",
    "WEBUI_DATABASE_URL": "APP_DATABASE_URL",
}

WEAK_PASSWORDS = {
    "",
    "admin123",
    "password",
    "changeme",
    "123456",
}


def _print_project_notice() -> None:
    for line in build_terminal_notice_lines():
        print(line)


def _apply_legacy_env_aliases(env: dict[str, str] | os._Environ[str] | None = None) -> None:
    target = env if env is not None else os.environ
    for legacy_name, canonical_name in LEGACY_ENV_ALIASES.items():
        if target.get(canonical_name):
            continue
        legacy_value = target.get(legacy_name)
        if legacy_value:
            target[canonical_name] = legacy_value


def _load_dotenv() -> None:
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return

    with open(env_path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _resolve_runtime_dir(env_key: str, default_name: str) -> Path:
    configured = str(os.environ.get(env_key) or "").strip()
    if configured:
        return Path(configured).expanduser()
    return PROJECT_ROOT / default_name


def _is_strong_access_password(password: str) -> bool:
    value = str(password or "").strip()
    if value.lower() in WEAK_PASSWORDS:
        return False
    if len(value) < 12:
        return False
    if not any(ch.isalpha() for ch in value):
        return False
    if not any(ch.isdigit() for ch in value):
        return False
    return True


def _enforce_startup_safety(settings) -> None:
    if bool(getattr(settings, "debug", False)):
        return

    password = getattr(settings, "webui_access_password").get_secret_value()
    if not _is_strong_access_password(password):
        raise SystemExit(
            "Refusing to start with a weak APP_ACCESS_PASSWORD. "
            "Set a strong password with at least 12 characters including letters and numbers."
        )


def _collect_runtime_overrides(args, env: dict[str, str] | os._Environ[str] | None = None) -> dict[str, object]:
    source = env if env is not None else os.environ
    overrides: dict[str, object] = {}

    host = args.host or source.get("APP_HOST")
    if host:
        overrides["webui_host"] = host

    port = args.port or source.get("APP_PORT")
    if port:
        overrides["webui_port"] = int(port)

    debug = bool(args.debug) or str(source.get("DEBUG", "")).lower() in ("1", "true", "yes")
    if debug:
        overrides["debug"] = debug

    log_level = args.log_level or source.get("LOG_LEVEL")
    if log_level:
        overrides["log_level"] = log_level

    access_password = args.access_password or source.get("APP_ACCESS_PASSWORD")
    if access_password:
        overrides["webui_access_password"] = access_password

    database_url = source.get("APP_DATABASE_URL") or source.get("DATABASE_URL")
    if database_url:
        overrides["database_url"] = database_url

    return overrides


def setup_application():
    apply_process_timezone()
    _load_dotenv()
    _apply_legacy_env_aliases()

    data_dir = _resolve_runtime_dir("APP_DATA_DIR", "data")
    logs_dir = _resolve_runtime_dir("APP_LOGS_DIR", "logs")
    data_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    os.environ["APP_DATA_DIR"] = str(data_dir)
    os.environ["APP_LOGS_DIR"] = str(logs_dir)
    if not os.environ.get("APP_DATABASE_URL"):
        os.environ["APP_DATABASE_URL"] = f"sqlite:///{(data_dir / 'database.db').resolve()}"

    initialize_database()
    settings = get_settings()
    _enforce_startup_safety(settings)

    log_file = str(logs_dir / Path(settings.log_file).name)
    setup_logging(log_level=settings.log_level, log_file=log_file)
    install_database_log_handler()

    logger = logging.getLogger(__name__)
    logger.info("数据库初始化完成，地基已经打好")
    logger.info("数据目录: %s", data_dir)
    logger.info("日志目录: %s", logs_dir)
    return settings


def start_webui() -> None:
    _print_project_notice()
    settings = setup_application()
    from src.web.app import app

    uvicorn.run(
        "src.web.app:app",
        host=settings.webui_host,
        port=settings.webui_port,
        reload=settings.debug,
        log_level="info" if settings.debug else "warning",
        access_log=settings.debug,
        ws="websockets",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OpenAI/Codex CLI 自动注册系统 Web UI")
    parser.add_argument("--host", help="监听主机 (优先级高于 APP_HOST，兼容旧 WEBUI_HOST)")
    parser.add_argument("--port", type=int, help="监听端口 (优先级高于 APP_PORT，兼容旧 WEBUI_PORT)")
    parser.add_argument("--debug", action="store_true", help="启用调试模式 (也可通过 DEBUG=1 设置)")
    parser.add_argument("--reload", action="store_true", help="启用热重载")
    parser.add_argument("--log-level", help="日志级别 (也可通过 LOG_LEVEL 设置)")
    parser.add_argument("--access-password", help="Web UI 访问密钥 (优先级高于 APP_ACCESS_PASSWORD，兼容旧 WEBUI_ACCESS_PASSWORD)")
    args = parser.parse_args(argv)

    _apply_legacy_env_aliases()
    updates = _collect_runtime_overrides(args)
    if updates:
        update_settings(**updates)

    start_webui()
    return 0


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    raise SystemExit(main())
