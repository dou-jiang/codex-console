"""Minimal API entrypoint for the migrated architecture."""

from fastapi import FastAPI


app = FastAPI(title="codex-platform-api")


@app.get("/health")
def health():
    return {"ok": True}
