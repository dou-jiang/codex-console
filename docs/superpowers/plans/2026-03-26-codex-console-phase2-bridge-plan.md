# codex-console Phase 2 Bridge Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce new/old dual-path drift by routing the remaining high-value legacy registration surfaces through the new task-based flow.

**Architecture:** Keep the new `apps/api` + `apps/worker` path as the execution source of truth. Bridge legacy single-task and task-observability endpoints to that path first, then tackle legacy batch entrypoints only after the old read surfaces no longer depend on the legacy runner. Avoid touching payment/browser flows in this plan.

**Tech Stack:** Python 3.10+, FastAPI, SQLAlchemy, pytest

---

## Remaining Work

### Priority 1: Old Observation Surfaces

- Legacy task list endpoint should read from the new task-backed store shape.
- Legacy task detail endpoint should expose the same status, result, and log view as the new API.
- Legacy task log endpoint should stop relying on the old in-memory-only path when durable logs are available.

### Priority 2: Old Execution Surfaces

- Legacy single-task `/start` is already bridged to the new flow.
- Legacy batch registration endpoints still use the old execution path and need a phased bridge.
- Legacy Outlook batch entrypoints still use the old execution path and should remain deferred until generic batch is bridged.

### Priority 3: Operational Hardening

- Expose clean startup entrypoints/scripts for API and worker.
- Keep the quickstart current.
- Reduce deprecated warnings only when they block migration momentum.

## Execution Order

### Task A: Bridge Legacy Task Read Endpoints

- [ ] Route legacy `/tasks`, `/tasks/{task_uuid}`, and `/tasks/{task_uuid}/logs` through shared serializers/store-backed reads.
- [ ] Keep response compatibility for the old UI while reusing the new data path.
- [ ] Add focused tests proving the legacy routes now reflect new task mutations.

### Task B: Validate Old/New Task Observability Coexistence

- [ ] Run focused old-route and new-route tests together.
- [ ] Verify new worker-written logs are visible through both paths.

### Task C: Bridge Legacy Batch Entry Creation

- [ ] Replace direct task creation inside legacy batch start with shared task creation helpers.
- [ ] Keep actual batch scheduling behavior stable until task creation is unified.
- [ ] Add tests for legacy batch task creation if the current suite lacks coverage.

### Task D: Bridge Legacy Batch Execution

- [ ] Move batch task execution dispatch onto the new worker/service primitives incrementally.
- [ ] Keep cancellation/status semantics intact.
- [ ] Defer Outlook-specific batch until generic batch is stable.

## Not In Scope

- Payment or bind-card flows
- Browser automation migration
- External queue brokers such as Redis/Celery/Temporal
- Frontend redesign

## Done Criteria

- Legacy single-task execution already uses the new task flow
- Legacy task read endpoints use the same task/result/log truth as the new API
- Legacy batch task creation begins using shared helpers
- Tests stay green after each bridge step
