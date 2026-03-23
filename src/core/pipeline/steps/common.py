from __future__ import annotations

from typing import Any

from src.core.pipeline.context import PipelineContext
from src.core.pipeline.proxy_preflight import choose_available_proxy


def get_registration_engine(ctx: PipelineContext) -> Any:
    engine = (ctx.metadata or {}).get("registration_engine")
    if engine is None:
        raise RuntimeError("registration_engine missing in PipelineContext.metadata")
    return engine


def get_proxy_ip_step(ctx: PipelineContext) -> dict[str, Any]:
    if ctx.proxy_url:
        return {}

    results = (ctx.metadata or {}).get("proxy_preflight_results") or []
    if not results:
        raise RuntimeError("proxy_preflight_results missing")

    selected = choose_available_proxy(results)
    proxy_url = str(selected.get("proxy_url") or "").strip()
    if not proxy_url:
        raise RuntimeError("selected proxy missing proxy_url")

    return {
        "proxy_url": proxy_url,
        "metadata": {
            "assigned_proxy_id": selected.get("proxy_id"),
        },
    }


def persist_account_step(ctx: PipelineContext) -> dict[str, Any]:
    # Task 4: pipeline adapters first. Persistence/survival wiring can be
    # completed in later tasks. Keep deterministic metadata for now.
    return {"metadata": {"persist_account_status": "deferred"}}


def schedule_survival_checks_step(ctx: PipelineContext) -> dict[str, Any]:
    return {"metadata": {"survival_checks_scheduled": False}}
