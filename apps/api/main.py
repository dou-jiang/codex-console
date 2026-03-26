"""Minimal API entrypoint for the migrated architecture."""

from pathlib import Path

from fastapi import FastAPI

from apps.api.routes.tasks import router as tasks_router
from packages.account_store.db import AccountStoreDB


def create_app(database_url: str = "sqlite:///./data/api.db") -> FastAPI:
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
