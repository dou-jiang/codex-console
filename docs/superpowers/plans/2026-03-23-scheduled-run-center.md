# Scheduled Run Center Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a same-page scheduled-run center for `/scheduled-tasks` with standalone run listing/filtering, detailed/incremental logs, soft-stop control, and richer staged progress logs for cleanup/refill runs.

**Architecture:** Extend `scheduled_runs` into the authoritative runtime record with stop-request and log-version metadata, then add a dedicated scheduled-run API surface on top of focused CRUD helpers. Keep the UI page-local by adding a second card for run history and a richer log modal that polls incrementally while a run is active. Implement soft-stop cooperatively inside the scheduler engine and runners so cancellation is safe and deterministic.

**Tech Stack:** FastAPI, SQLAlchemy ORM, SQLite/PostgreSQL migration helpers, vanilla JS, pytest, FastAPI TestClient, Node syntax checks

---

## File Map

- `src/database/models.py` — extend `ScheduledRun` with stop/log metadata fields.
- `src/database/session.py` — add SQLite/PostgreSQL migration entries for new `scheduled_runs` columns.
- `src/database/crud.py` — add scheduled-run query/filter/detail/log-offset/stop helpers and update log append semantics.
- `src/scheduler/schemas.py` — define new scheduled-run list/detail/log schemas and include new status semantics.
- `src/scheduler/run_logger.py` — centralize log appends so `last_log_at` / `log_version` stay consistent.
- `src/scheduler/engine.py` — add stop-request plumbing, cancellation exception, and run-task-type persistence.
- `src/scheduler/runners/cleanup.py` — add staged progress logs and soft-stop checkpoints.
- `src/scheduler/runners/refill.py` — add staged progress logs and soft-stop checkpoints.
- `src/scheduler/runners/refresh.py` — add soft-stop checkpoints.
- `src/web/routes/scheduled_tasks.py` — expose global run list/detail/log/stop APIs and keep existing plan APIs compatible.
- `templates/scheduled_tasks.html` — add standalone run-center card, filters, and richer log modal controls.
- `static/js/scheduled_tasks.js` — load/filter/render run list, open detail/log modal, poll incremental logs, and submit stop requests.
- `tests/test_database_session.py` — migration coverage for new `scheduled_runs` columns.
- `tests/test_scheduler_data_model.py` — model/CRUD coverage for run metadata and log append/version behavior.
- `tests/test_scheduled_tasks_routes.py` — route coverage for run list/detail/incremental logs/stop.
- `tests/test_scheduler_engine.py` — engine-level stop/cancel behavior tests.
- `tests/test_scheduler_cleanup_runner.py` — cleanup staged logs and stop-check behavior.
- `tests/test_scheduler_refill_runner.py` — refill staged logs and stop-check behavior.
- `tests/test_scheduler_refresh_runner.py` — refresh stop-check behavior.
- `tests/test_scheduled_tasks_page_assets.py` — asset coverage for run-center markup/hooks/live-log logic.

## Task 1: Extend scheduled run persistence and log metadata

**Files:**
- Modify: `src/database/models.py`
- Modify: `src/database/session.py`
- Modify: `src/database/crud.py`
- Test: `tests/test_database_session.py`
- Test: `tests/test_scheduler_data_model.py`

- [ ] **Step 1: Write migration/model tests for new `scheduled_runs` fields**

Add failing assertions for:
- SQLite/PostgreSQL migration statements creating `task_type`, `stop_requested_at`, `stop_requested_by`, `stop_reason`, `last_log_at`, `log_version`
- ORM inspector checks showing the new columns exist
- CRUD round-trip checks verifying new runs default `log_version=0`, stop fields are `None`

Example assertions to add:

```python
assert "task_type" in scheduled_run_columns
assert "log_version" in scheduled_run_columns
assert run.log_version == 0
assert run.stop_requested_at is None
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run:

```bash
pytest tests/test_database_session.py tests/test_scheduler_data_model.py -q
```

Expected: FAIL because the new scheduled-run columns and defaults do not exist yet.

- [ ] **Step 3: Implement model, migration, and CRUD defaults**

Make these changes:
- Add the six new columns to `ScheduledRun` in `src/database/models.py`.
- Add matching SQLite/PostgreSQL migration entries in `src/database/session.py`.
- Update `crud.create_scheduled_run()` to accept/persist `task_type` and initialize `log_version=0`.
- Update `crud.append_scheduled_run_log()` to increment `log_version` and set `last_log_at=datetime.utcnow()`.
- Add focused CRUD helpers now needed later:
  - `get_scheduled_run_by_id(...)`
  - `mark_scheduled_run_stop_requested(...)`
  - `get_scheduled_run_log_chunk(...)`
  - `get_scheduled_runs(...)` with filter parameters

- [ ] **Step 4: Re-run the focused tests and make them pass**

Run:

```bash
pytest tests/test_database_session.py tests/test_scheduler_data_model.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit the persistence layer work**

```bash
git add src/database/models.py src/database/session.py src/database/crud.py tests/test_database_session.py tests/test_scheduler_data_model.py
git commit -m "feat: extend scheduled run persistence"
```

## Task 2: Add scheduled-run API schemas and route surface

**Files:**
- Modify: `src/scheduler/schemas.py`
- Modify: `src/web/routes/scheduled_tasks.py`
- Modify: `src/database/crud.py`
- Test: `tests/test_scheduled_tasks_routes.py`

- [ ] **Step 1: Write failing route tests for the new run-center APIs**

Add tests covering:
- `GET /api/scheduled-runs` filters by `task_type`, `status`, and time range
- `GET /api/scheduled-runs/{run_id}` returns run + plan summary + `can_stop`
- `GET /api/scheduled-runs/{run_id}/logs?offset=...` returns `chunk`, `next_offset`, `is_running`
- `POST /api/scheduled-runs/{run_id}/stop` sets stop request fields for a `running` run
- stopping a finished run rejects correctly

Example test shape:

```python
response = client.get("/api/scheduled-runs", params={"task_type": "cpa_cleanup"})
assert response.status_code == 200
assert response.json()["total"] == 1
```

- [ ] **Step 2: Run the focused route tests to verify they fail**

Run:

```bash
pytest tests/test_scheduled_tasks_routes.py -q
```

Expected: FAIL because the new scheduled-run endpoints and schemas do not exist yet.

- [ ] **Step 3: Implement schemas and route handlers**

Make these changes:
- Add new schema models in `src/scheduler/schemas.py`:
  - scheduled-run list item/detail response
  - incremental log response
  - stop response
- In `src/web/routes/scheduled_tasks.py`, add:
  - `GET /scheduled-runs`
  - `GET /scheduled-runs/{run_id}`
  - `GET /scheduled-runs/{run_id}/logs`
  - `POST /scheduled-runs/{run_id}/stop`
- Reuse existing plan-run endpoints for backward compatibility.
- Ensure `can_stop` is true only when `status == "running"` and no stop has been requested.

- [ ] **Step 4: Re-run the route tests and make them pass**

Run:

```bash
pytest tests/test_scheduled_tasks_routes.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit the API surface work**

```bash
git add src/scheduler/schemas.py src/web/routes/scheduled_tasks.py src/database/crud.py tests/test_scheduled_tasks_routes.py
git commit -m "feat: add scheduled run center APIs"
```

## Task 3: Add scheduler-engine stop plumbing and cancellation semantics

**Files:**
- Modify: `src/scheduler/engine.py`
- Modify: `src/scheduler/run_logger.py`
- Modify: `src/database/crud.py`
- Test: `tests/test_scheduler_engine.py`

- [ ] **Step 1: Write failing engine tests for stop requests and cancelled completion**

Add tests for:
- engine/manual-stop helper marks a running run with `stop_requested_at`
- unsupported/finished runs cannot be stop-requested as running work
- a runner that raises the cancellation exception finalizes as `cancelled`
- `task_type` is written to the run when dispatch starts

Example test shape:

```python
run_id = engine.trigger_plan_now(plan.id)
engine.request_run_stop(run_id)
run = temp_db.query(ScheduledRun).get(run_id)
assert run.stop_requested_at is not None
```

- [ ] **Step 2: Run the engine suite to verify it fails**

Run:

```bash
pytest tests/test_scheduler_engine.py -q
```

Expected: FAIL because stop-request helpers and cancelled semantics do not exist yet.

- [ ] **Step 3: Implement engine stop and cancellation behavior**

Make these changes:
- Introduce a dedicated cancellation exception, e.g. `ScheduledRunCancelledError`.
- Add engine helper(s) such as:
  - `request_run_stop(run_id)`
  - `is_run_stop_requested(run_id)`
- Persist `task_type` when creating runs in `_create_run_and_mark_started()`.
- Update worker finalization so cancellation becomes:
  - `status="cancelled"`
  - `error_message="user requested stop"`
- Keep `last_run_status` on the plan in sync with cancelled runs.
- Add a reusable `append_run_log(..., timestamp update)` flow through `run_logger.py`.

- [ ] **Step 4: Re-run the engine tests and make them pass**

Run:

```bash
pytest tests/test_scheduler_engine.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit the engine stop plumbing**

```bash
git add src/scheduler/engine.py src/scheduler/run_logger.py src/database/crud.py tests/test_scheduler_engine.py
git commit -m "feat: add scheduled run stop semantics"
```

## Task 4: Add staged progress logs and stop checkpoints to runners

**Files:**
- Modify: `src/scheduler/runners/cleanup.py`
- Modify: `src/scheduler/runners/refill.py`
- Modify: `src/scheduler/runners/refresh.py`
- Modify: `src/scheduler/run_logger.py`
- Test: `tests/test_scheduler_cleanup_runner.py`
- Test: `tests/test_scheduler_refill_runner.py`
- Test: `tests/test_scheduler_refresh_runner.py`

- [ ] **Step 1: Write failing runner tests for staged logs and cooperative stop**

Add failing tests that verify:
- cleanup logs probe/expire/delete progress as counts grow
- refill logs target resolution and every-20 progress summaries
- cleanup/refill/refresh exit with `cancelled` when stop is requested mid-loop
- cancelled runs append explicit user-stop log lines

Example assertions:

```python
assert "cleanup probe progress" in (persisted_run.logs or "")
assert "refill progress" in (persisted_run.logs or "")
assert persisted_run.status == "cancelled"
```

- [ ] **Step 2: Run the runner suites to verify they fail**

Run:

```bash
pytest tests/test_scheduler_cleanup_runner.py tests/test_scheduler_refill_runner.py tests/test_scheduler_refresh_runner.py -q
```

Expected: FAIL because staged progress logs and stop checks do not exist yet.

- [ ] **Step 3: Implement cleanup progress checkpoints**

In `cleanup.py`:
- add stop checks after probe callbacks and before local/remote item processing
- emit progress every 100 accounts for probe/expire/delete loops
- when stop is requested, log both “收到停止请求” and “任务已按请求停止” semantics before finalizing as cancelled

- [ ] **Step 4: Implement refill/refresh cooperative stop and progress logs**

In `refill.py`:
- keep the existing target-resolution log
- emit `refill progress (...)` every 20 attempts/success milestones
- check stop at each loop boundary, after registration, and after upload

In `refresh.py`:
- add stop checks around each account stage
- finalize as cancelled on stop without marking unrelated failures

- [ ] **Step 5: Re-run the runner suites and make them pass**

Run:

```bash
pytest tests/test_scheduler_cleanup_runner.py tests/test_scheduler_refill_runner.py tests/test_scheduler_refresh_runner.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit the runner behavior changes**

```bash
git add src/scheduler/runners/cleanup.py src/scheduler/runners/refill.py src/scheduler/runners/refresh.py src/scheduler/run_logger.py tests/test_scheduler_cleanup_runner.py tests/test_scheduler_refill_runner.py tests/test_scheduler_refresh_runner.py
git commit -m "feat: add scheduled runner progress and stop checks"
```

## Task 5: Build the same-page run center UI and live log modal

**Files:**
- Modify: `templates/scheduled_tasks.html`
- Modify: `static/js/scheduled_tasks.js`
- Test: `tests/test_scheduled_tasks_page_assets.py`

- [ ] **Step 1: Write failing asset tests for the run-center UI hooks**

Add tests that verify the template/script contain:
- run list card and filter inputs
- global run-table body hooks
- run detail/log modal controls
- live log polling helpers
- stop button handlers and `stopping` UI markers

Example assertions:

```python
assert 'id="scheduled-runs-table"' in template
assert "function loadScheduledRuns(" in script
assert "setInterval(" in script and "scheduled-runs" in script
assert 'data-action="stop-run"' in script
```

- [ ] **Step 2: Run the asset suite to verify it fails**

Run:

```bash
pytest tests/test_scheduled_tasks_page_assets.py -q
```

Expected: FAIL because the run-center markup and hooks do not exist yet.

- [ ] **Step 3: Implement the template additions**

In `templates/scheduled_tasks.html`:
- add a second card for run history below the plan list
- add filter controls for task type/status/start range
- extend the existing log modal with status bar, refresh button, auto-scroll toggle, and stop button area
- keep existing IDs stable where possible

- [ ] **Step 4: Implement page JS for run list, detail, polling, and stop**

In `static/js/scheduled_tasks.js`:
- add state/cache for run filters, active run detail, current log offset, polling timer
- add `loadScheduledRuns()`, `renderScheduledRuns()`, `buildScheduledRunQuery()`
- wire plan-row “记录” buttons to filter the run list by `plan_id`
- add `openScheduledRunDetail(runId)` / `openScheduledRunLog(runId)`
- poll `/api/scheduled-runs/{run_id}/logs?offset=...` every 2s while `is_running`
- stop polling automatically when the run reaches a terminal state
- add `stopScheduledRun(runId)` with button busy guard and “停止中” UI feedback

- [ ] **Step 5: Re-run the asset tests and pass them**

Run:

```bash
pytest tests/test_scheduled_tasks_page_assets.py -q
node --check static/js/scheduled_tasks.js
```

Expected: both pass.

- [ ] **Step 6: Commit the UI work**

```bash
git add templates/scheduled_tasks.html static/js/scheduled_tasks.js tests/test_scheduled_tasks_page_assets.py
git commit -m "feat: add scheduled run center UI"
```

## Task 6: End-to-end verification and documentation polish

**Files:**
- Verify only: touched source, template, JS, and tests
- Optional doc touch if needed: `README.md`

- [ ] **Step 1: Run all focused scheduler/UI suites**

Run:

```bash
pytest tests/test_database_session.py tests/test_scheduler_data_model.py tests/test_scheduled_tasks_routes.py tests/test_scheduler_engine.py tests/test_scheduler_cleanup_runner.py tests/test_scheduler_refill_runner.py tests/test_scheduler_refresh_runner.py tests/test_scheduled_tasks_page_assets.py -q
```

Expected: PASS.

- [ ] **Step 2: Run the broader regression suite**

Run:

```bash
pytest -q
```

Expected: PASS.

- [ ] **Step 3: Perform a real UI smoke check for the new run center**

Verify in an isolated temp DB/runtime:
- create/trigger a plan
- observe the run appear in the standalone run list
- open logs while running and confirm incremental updates
- request stop on a long-running run and confirm `stopping -> cancelled`
- verify cleanup/refill staged logs are visible

Record the evidence paths (JSON/screenshot/temp DB) before claiming success.

- [ ] **Step 4: Update docs only if the shipped UX changed materially**

If the new run-center controls need user-facing explanation, add a short README section describing:
- where to find the run list
- how live logs work
- what “停止中” means

- [ ] **Step 5: Commit the verification/doc follow-up**

```bash
git add README.md
# only if README changed
git commit -m "docs: describe scheduled run center"
```

