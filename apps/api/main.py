"""Minimal API entrypoint for the migrated architecture."""

import os
import argparse
from pathlib import Path

from fastapi import FastAPI
import uvicorn

from apps.api.routes.tasks import router as tasks_router
from packages.account_store.db import AccountStoreDB


def create_app(database_url: str | None = None) -> FastAPI:
    if database_url is None:
        database_url = os.environ.get("APP_DATABASE_URL", "sqlite:///./data/api.db")
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
