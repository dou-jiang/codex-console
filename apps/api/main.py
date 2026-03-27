"""Minimal API entrypoint for the migrated architecture."""

import os
import argparse
from pathlib import Path

from fastapi import FastAPI
import uvicorn

from apps.api.routes.tasks import router as tasks_router
from packages.account_store.db import AccountStoreDB
from src.database.init_db import initialize_database
from src.config import settings as settings_module
from src.database import session as session_module


PROJECT_ROOT = Path(__file__).resolve().parents[2]

LEGACY_ENV_ALIASES = {
    "WEBUI_HOST": "APP_HOST",
    "WEBUI_PORT": "APP_PORT",
    "WEBUI_ACCESS_PASSWORD": "APP_ACCESS_PASSWORD",
    "WEBUI_DATABASE_URL": "APP_DATABASE_URL",
}


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


def _apply_legacy_env_aliases() -> None:
    for legacy_name, canonical_name in LEGACY_ENV_ALIASES.items():
        if os.environ.get(canonical_name):
            continue
        legacy_value = os.environ.get(legacy_name)
        if legacy_value:
            os.environ[canonical_name] = legacy_value


def _bootstrap_runtime(database_url: str | None) -> str:
    _load_dotenv()
    _apply_legacy_env_aliases()

    resolved_database_url = database_url or os.environ.get("APP_DATABASE_URL") or "sqlite:///./data/api.db"
    os.environ["APP_DATABASE_URL"] = resolved_database_url

    current_manager = getattr(session_module, "_db_manager", None)
    if current_manager is not None and getattr(current_manager, "database_url", None) != resolved_database_url:
        session_module._db_manager = None

    settings_module._settings = None
    initialize_database(resolved_database_url)
    settings_module._settings = None
    settings_module.get_settings()
    return resolved_database_url


def create_app(database_url: str | None = None) -> FastAPI:
    database_url = _bootstrap_runtime(database_url)
    if database_url.startswith("sqlite:///"):
        db_path = Path(database_url.replace("sqlite:///", "", 1))
        db_path.parent.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="codex-platform-api")
    app.state.store = AccountStoreDB(database_url=database_url)

    @app.get("/health")
    def health():
        return {"ok": True}

    app.include_router(tasks_router)
    return app


app = create_app()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the migrated API app.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--database-url", default="sqlite:///./data/api.db")
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args(argv)

    # Keep a stable import target for uvicorn and supply database_url through env.
    os.environ["APP_DATABASE_URL"] = args.database_url
    uvicorn.run(
        "apps.api.main:create_app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0
