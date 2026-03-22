# Registration Unlimited Mode and Domain Stats Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expand registration batch counts to `1-500`, add a dedicated unlimited registration mode (`count=0`), automatically stop unlimited runs after more than 10 consecutive failures, and show sorted per-domain success/failure stats when batch-style tasks finish.

**Architecture:** Keep `/registration/start` and `/registration/batch` as the public API, but add a small pure helper module for batch metrics/domain aggregation so the route layer is not forced to hand-roll streak logic. Persist each task’s generated email on `registration_tasks`, extend in-memory batch state with unlimited-mode metadata, and let the frontend branch on explicit `is_unlimited` status fields instead of inferring behavior from `total`.

**Tech Stack:** FastAPI, Pydantic, SQLAlchemy ORM, SQLite/PostgreSQL auto-migrations, vanilla JS, Jinja2 templates, existing task manager + WebSocket flow, pytest, Node-based JS harnesses

---

## Implementation Notes Before Starting

- Use the approved spec as the source of truth:
  - `docs/superpowers/specs/2026-03-22-registration-unlimited-mode-and-domain-stats-design.md`
- If you are implementing instead of only planning, create/use a dedicated worktree before touching code.
- Keep the public API surface narrow:
  - single registration stays on `POST /registration/start`
  - batch + unlimited both stay on `POST /registration/batch`
  - unlimited semantics are `count=0`
- Do not add new third-party dependencies.
- Reuse the existing `.progress-bar.indeterminate` CSS instead of inventing a second infinite-progress style.
- Keep Outlook’s dedicated batch entrypoint intact; only add the shared end-of-batch domain stats exposure it inherits from common batch state.
- Preserve existing cancel semantics: stop dispatching new work, let already-running tasks finish, then finalize the batch.

## File Structure Map

### Existing files to modify

- `src/database/models.py:102-116`
  - Add `email_address` to `RegistrationTask`
- `src/database/session.py:18-30`
  - Add `registration_tasks.email_address` to SQLite/PostgreSQL auto-migration lists
- `src/database/crud.py:244-299`
  - Let task create/update helpers read/write `email_address`
- `src/web/task_manager.py:214-323`
  - Track `is_unlimited`, `consecutive_failures`, `max_consecutive_failures`, `stop_reason`, and `domain_stats`
- `src/web/routes/registration.py:82-119,581-958,1318-1519`
  - Update request/response models, count validation, fixed-batch finalization, unlimited batch runner, and status payloads
- `templates/index.html:234-363`
  - Add `无限注册` mode option, unlimited status placeholders, and the domain-stats container
- `static/js/app.js:429-706,884-911,1437-1500`
  - Handle the new mode, send `count=0`, render indeterminate progress + consecutive failures, persist/restore active unlimited batches, and render domain stats
- `tests/test_database_session.py`
  - Cover the new migration column

### New files to create

- `src/core/registration_batch_metrics.py`
  - Pure helpers for batch counters/streak handling and sorted domain-stat aggregation
- `tests/test_registration_batch_metrics.py`
  - Unit tests for streak-reset and domain-stat sorting rules
- `tests/test_registration_batch_routes.py`
  - Async route/runner tests for count validation, task email persistence, fixed-batch stats, unlimited auto-stop, and status payloads
- `tests/test_registration_page_assets.py`
  - Template + app.js smoke tests for the new mode and rendering hooks
- `tests_runtime/app_js_harness.py`
  - Node-based harness for `static/js/app.js` scenarios (mode switch, request payload, unlimited progress, domain stats)

### Task 1: Build pure batch-metrics helpers first

**Files:**
- Create: `tests/test_registration_batch_metrics.py`
- Create: `src/core/registration_batch_metrics.py`
- Reference: `docs/superpowers/specs/2026-03-22-registration-unlimited-mode-and-domain-stats-design.md`

- [ ] **Step 1: Write the failing metrics tests**

```python
from types import SimpleNamespace

from src.core.registration_batch_metrics import apply_task_outcome, build_domain_stats


def test_apply_task_outcome_resets_consecutive_failures_after_success():
    state = {"completed": 0, "success": 0, "failed": 0, "consecutive_failures": 0}

    for status in ["failed", "failed", "completed", "failed"]:
        apply_task_outcome(state, status)

    assert state == {
        "completed": 4,
        "success": 1,
        "failed": 3,
        "consecutive_failures": 1,
    }


def test_build_domain_stats_sorts_by_success_rate_then_volume_and_puts_missing_email_last():
    tasks = [
        SimpleNamespace(status="completed", email_address="a@yahoo.com"),
        SimpleNamespace(status="completed", email_address="b@gmail.com"),
        SimpleNamespace(status="failed", email_address="c@gmail.com"),
        SimpleNamespace(status="failed", email_address=None),
    ]

    stats = build_domain_stats(tasks)

    assert [row["domain"] for row in stats] == ["yahoo.com", "gmail.com", "未获取邮箱"]
    assert stats[1]["success_rate"] == 50.0
    assert stats[2]["failure_rate"] == 100.0
```

- [ ] **Step 2: Run the new test file and verify it fails**

Run: `pytest tests/test_registration_batch_metrics.py -v`

Expected: FAIL because `src.core.registration_batch_metrics` does not exist yet.

- [ ] **Step 3: Implement the minimal pure helpers**

```python
MISSING_EMAIL_DOMAIN = "未获取邮箱"


def apply_task_outcome(state: dict[str, int], status: str) -> None:
    state["completed"] += 1
    if status == "completed":
        state["success"] += 1
        state["consecutive_failures"] = 0
    elif status == "failed":
        state["failed"] += 1
        state["consecutive_failures"] += 1


def build_domain_stats(tasks) -> list[dict[str, float | int | str]]:
    # group by normalized domain or MISSING_EMAIL_DOMAIN
    # compute total/success/failed and percentage fields
    # sort by success_rate desc, total desc, domain asc, missing-email last
    return stats
```

Implementation notes:
- Treat only `completed` and `failed` tasks as input; callers should filter before passing.
- Normalize emails case-insensitively.
- Round percentage fields consistently (e.g. one decimal place or float with deterministic formatting — pick one and keep it consistent in tests + UI).

- [ ] **Step 4: Re-run the metrics tests**

Run: `pytest tests/test_registration_batch_metrics.py -v`

Expected: PASS for both streak and domain-stat ordering rules.

- [ ] **Step 5: Commit the helper baseline**

```bash
git add tests/test_registration_batch_metrics.py src/core/registration_batch_metrics.py
git commit -m "feat: add registration batch metrics helpers"
```

### Task 2: Add `email_address` persistence on registration tasks

**Files:**
- Modify: `src/database/models.py:102-116`
- Modify: `src/database/session.py:18-30`
- Modify: `src/database/crud.py:244-299`
- Modify: `tests/test_database_session.py`
- Create: `tests/test_registration_batch_routes.py`

- [ ] **Step 1: Write the failing migration + CRUD tests**

```python
from src.database import crud


def test_postgresql_migrate_tables_adds_registration_task_email_column(monkeypatch):
    ...
    assert any(
        'ALTER TABLE "registration_tasks" ADD COLUMN IF NOT EXISTS "email_address" VARCHAR(255)' in stmt
        for stmt in connection.statements
    )


def test_create_and_update_registration_task_persist_email_address(temp_db):
    task = crud.create_registration_task(
        temp_db,
        task_uuid="task-1",
        email_address="first@gmail.com",
    )

    updated = crud.update_registration_task(
        temp_db,
        "task-1",
        email_address="second@gmail.com",
    )

    assert task.email_address == "first@gmail.com"
    assert updated.email_address == "second@gmail.com"
```

Fixture note for `tests/test_registration_batch_routes.py`:
- Reuse the `DatabaseSessionManager` + `Base.metadata.create_all()` pattern already used in `tests/test_proxy_settings_routes.py`.

- [ ] **Step 2: Run just the new/updated tests and verify they fail**

Run: `pytest tests/test_database_session.py tests/test_registration_batch_routes.py -k "email_address or migrate_tables" -v`

Expected: FAIL because the model/migration/helper signatures do not expose `email_address` yet.

- [ ] **Step 3: Implement the schema + helper support**

```python
class RegistrationTask(Base):
    ...
    email_address = Column(String(255), index=True)


SQLITE_MIGRATIONS = [
    ...,
    ("registration_tasks", "email_address", "VARCHAR(255)"),
]

POSTGRESQL_MIGRATIONS = [
    ...,
    ("registration_tasks", "email_address", "VARCHAR(255)"),
]


def create_registration_task(..., email_address: Optional[str] = None, ...):
    db_task = RegistrationTask(..., email_address=email_address)
```

Implementation notes:
- `update_registration_task()` already supports generic kwargs once the model has the attribute; verify that behavior stays intact.
- Do not add a separate migration framework; stay within the existing auto-migration pattern.

- [ ] **Step 4: Re-run the migration + CRUD tests**

Run: `pytest tests/test_database_session.py tests/test_registration_batch_routes.py -k "email_address or migrate_tables" -v`

Expected: PASS with the new column created and helper persistence working.

- [ ] **Step 5: Commit the persistence foundation**

```bash
git add src/database/models.py src/database/session.py src/database/crud.py tests/test_database_session.py tests/test_registration_batch_routes.py
git commit -m "feat: persist registration task email addresses"
```

### Task 3: Scaffold unlimited-batch request/state fields before implementing the runner

**Files:**
- Modify: `src/web/task_manager.py:214-323`
- Modify: `src/web/routes/registration.py:82-119,581-958`
- Modify: `tests/test_registration_batch_routes.py`

- [ ] **Step 1: Add failing route/state tests for the new batch metadata**

```python
import asyncio

import pytest
from fastapi import BackgroundTasks, HTTPException

from src.web.routes import registration as registration_routes


class FakeTaskManager:
    def __init__(self):
        self._status = {}

    def init_batch(self, batch_id, total, **extra):
        self._status[batch_id] = {"status": "running", "total": total, **extra}

    def update_batch_status(self, batch_id, **kwargs):
        self._status.setdefault(batch_id, {}).update(kwargs)

    def get_batch_status(self, batch_id):
        return self._status.get(batch_id)

    def is_batch_cancelled(self, batch_id):
        return self._status.get(batch_id, {}).get("cancelled", False)


def test_start_batch_registration_accepts_zero_and_queues_unlimited_runner(route_db, monkeypatch):
    monkeypatch.setattr(registration_routes, "task_manager", FakeTaskManager())
    background = BackgroundTasks()

    response = asyncio.run(
        registration_routes.start_batch_registration(
            registration_routes.BatchRegistrationRequest(count=0, email_service_type="tempmail"),
            background,
        )
    )

    assert response.count == 0
    assert response.is_unlimited is True
    assert response.tasks == []
    assert background.tasks[0].func is registration_routes.run_unlimited_batch_registration


def test_start_batch_registration_rejects_counts_outside_zero_to_500():
    with pytest.raises(HTTPException):
        asyncio.run(
            registration_routes.start_batch_registration(
                registration_routes.BatchRegistrationRequest(count=501, email_service_type="tempmail"),
                BackgroundTasks(),
            )
        )


def test_get_batch_status_includes_unlimited_metadata(monkeypatch):
    registration_routes.batch_tasks["batch-1"] = {
        "total": 0,
        "completed": 3,
        "success": 1,
        "failed": 2,
        "cancelled": False,
        "finished": False,
        "current_index": 3,
        "is_unlimited": True,
        "consecutive_failures": 2,
        "max_consecutive_failures": 10,
        "stop_reason": None,
        "domain_stats": [],
    }

    result = asyncio.run(registration_routes.get_batch_status("batch-1"))

    assert result["is_unlimited"] is True
    assert result["consecutive_failures"] == 2
    assert result["max_consecutive_failures"] == 10
    assert result["stop_reason"] is None
```

- [ ] **Step 2: Run the focused route tests and confirm they fail**

Run: `pytest tests/test_registration_batch_routes.py -k "zero_and_queues_unlimited_runner or zero_to_500 or unlimited_metadata" -v`

Expected: FAIL because the response/state models do not yet know about unlimited fields or `count=0`.

- [ ] **Step 3: Implement the minimum request/response/state scaffolding**

```python
class BatchRegistrationResponse(BaseModel):
    batch_id: str
    count: int
    is_unlimited: bool = False
    tasks: List[RegistrationTaskResponse]


def _init_batch_state(batch_id: str, task_uuids: list[str], *, is_unlimited: bool = False, total: int | None = None):
    computed_total = 0 if is_unlimited else (total if total is not None else len(task_uuids))
    task_manager.init_batch(
        batch_id,
        computed_total,
        is_unlimited=is_unlimited,
        consecutive_failures=0,
        max_consecutive_failures=10,
        stop_reason=None,
        domain_stats=[],
    )
    batch_tasks[batch_id] = {
        ...,
        "total": computed_total,
        "is_unlimited": is_unlimited,
        "consecutive_failures": 0,
        "max_consecutive_failures": 10,
        "stop_reason": None,
        "domain_stats": [],
    }
```

Implementation notes:
- Update the `count` validation from `1-100` to `0-500`.
- Keep ordinary fixed batch responses unchanged except for the new explicit `is_unlimited=False` field.
- Returning an empty `tasks` list for unlimited starts is acceptable; the UI only needs `batch_id` + mode metadata.

- [ ] **Step 4: Re-run the focused route tests**

Run: `pytest tests/test_registration_batch_routes.py -k "zero_and_queues_unlimited_runner or zero_to_500 or unlimited_metadata" -v`

Expected: PASS with the new schema/state fields in place.

- [ ] **Step 5: Commit the unlimited-state scaffolding**

```bash
git add src/web/task_manager.py src/web/routes/registration.py tests/test_registration_batch_routes.py
git commit -m "feat: scaffold unlimited registration batch state"
```

### Task 4: Implement task email capture, fixed-batch finalization, and the unlimited runner

**Files:**
- Modify: `src/web/routes/registration.py:224-958,1318-1519`
- Modify: `src/web/task_manager.py:214-323`
- Modify: `src/core/registration_batch_metrics.py`
- Modify: `tests/test_registration_batch_routes.py`

- [ ] **Step 1: Add failing worker/runner tests for email capture, final stats, and stop-after-11 behavior**

```python
from datetime import datetime
from types import SimpleNamespace

from src.database import crud


def test_run_sync_registration_task_persists_email_address_even_on_failure(route_db, monkeypatch):
    crud.create_registration_task(route_db, task_uuid="task-1")

    monkeypatch.setattr(
        registration_routes,
        "get_settings",
        lambda: SimpleNamespace(tempmail_base_url="http://mail", tempmail_timeout=1, tempmail_max_retries=0),
    )
    monkeypatch.setattr(
        registration_routes.EmailServiceFactory,
        "create",
        lambda *args, **kwargs: SimpleNamespace(service_type=SimpleNamespace(value="tempmail")),
    )

    class FakeEngine:
        def __init__(self, **kwargs):
            pass

        def run(self):
            return SimpleNamespace(
                success=False,
                email="failed@gmail.com",
                error_message="boom",
                to_dict=lambda: {"email": "failed@gmail.com"},
            )

    monkeypatch.setattr(registration_routes, "RegistrationEngine", FakeEngine)

    registration_routes._run_sync_registration_task("task-1", "tempmail", None, None)

    task = crud.get_registration_task(route_db, "task-1")
    assert task.status == "failed"
    assert task.email_address == "failed@gmail.com"


def test_run_batch_registration_attaches_sorted_domain_stats(route_db, fake_task_manager, monkeypatch):
    task_ids = []
    for name in ["a", "b", "c"]:
        task = crud.create_registration_task(route_db, task_uuid=f"task-{{name}}")
        task_ids.append(task.task_uuid)

    outcomes = iter([
        ("completed", "one@yahoo.com"),
        ("failed", "two@gmail.com"),
        ("completed", "three@gmail.com"),
    ])

    async def fake_run_registration_task(task_uuid, *args, **kwargs):
        status, email = next(outcomes)
        crud.update_registration_task(
            route_db,
            task_uuid,
            status=status,
            email_address=email,
            completed_at=datetime.utcnow(),
            result={"email": email} if status == "completed" else None,
            error_message=None if status == "completed" else "boom",
        )

    monkeypatch.setattr(registration_routes, "run_registration_task", fake_run_registration_task)

    asyncio.run(
        registration_routes.run_batch_registration(
            batch_id="fixed-1",
            task_uuids=task_ids,
            email_service_type="tempmail",
            proxy=None,
            email_service_config=None,
            email_service_id=None,
            interval_min=0,
            interval_max=0,
            concurrency=1,
            mode="parallel",
        )
    )

    stats = registration_routes.batch_tasks["fixed-1"]["domain_stats"]
    assert [row["domain"] for row in stats] == ["yahoo.com", "gmail.com"]


def test_run_unlimited_batch_registration_stops_after_eleven_consecutive_failures(route_db, fake_task_manager, monkeypatch):
    outcomes = iter([("failed", f"user{{i}}@bad.com") for i in range(11)] + [("completed", "late@good.com")])

    async def fake_run_registration_task(task_uuid, *args, **kwargs):
        status, email = next(outcomes)
        crud.update_registration_task(
            route_db,
            task_uuid,
            status=status,
            email_address=email,
            completed_at=datetime.utcnow(),
            result={"email": email} if status == "completed" else None,
            error_message=None if status == "completed" else "boom",
        )

    monkeypatch.setattr(registration_routes, "run_registration_task", fake_run_registration_task)

    asyncio.run(
        registration_routes.run_unlimited_batch_registration(
            batch_id="unlimited-1",
            email_service_type="tempmail",
            proxy=None,
            email_service_config=None,
            email_service_id=None,
            interval_min=0,
            interval_max=0,
            concurrency=1,
            mode="parallel",
        )
    )

    state = registration_routes.batch_tasks["unlimited-1"]
    assert state["completed"] == 11
    assert state["failed"] == 11
    assert state["consecutive_failures"] == 11
    assert state["stop_reason"] == "too_many_consecutive_failures"
```

Optional lightweight regression to add in the same file:
- `get_outlook_batch_status()` should forward `domain_stats` if present on shared batch state.

- [ ] **Step 2: Run those focused tests and confirm they fail**

Run: `pytest tests/test_registration_batch_routes.py -k "persists_email_address_even_on_failure or attaches_sorted_domain_stats or eleven_consecutive_failures" -v`

Expected: FAIL because worker persistence, batch finalization, and the unlimited runner are not implemented yet.

- [ ] **Step 3: Implement the worker + finalization logic with the new unlimited runner**

```python
def _finalize_batch_domain_stats(batch_id: str, task_uuids: list[str]) -> None:
    with get_db() as db:
        tasks = (
            db.query(RegistrationTask)
            .filter(RegistrationTask.task_uuid.in_(task_uuids))
            .filter(RegistrationTask.status.in_(["completed", "failed"]))
            .all()
        )
    stats = build_domain_stats(tasks)
    batch_tasks[batch_id]["domain_stats"] = stats
    task_manager.update_batch_status(batch_id, domain_stats=stats)


# inside _run_sync_registration_task, immediately after result = engine.run()
if getattr(result, "email", ""):
    crud.update_registration_task(db, task_uuid, email_address=result.email)


async def run_unlimited_batch_registration(...):
    _init_batch_state(batch_id, [], is_unlimited=True, total=0)
    # loop: create one DB task UUID at a time, run up to `concurrency`,
    # call apply_task_outcome() when each task finishes,
    # stop dispatch when cancelled or consecutive_failures > max_consecutive_failures,
    # wait for in-flight tasks, then finalize domain stats + finished status.
```

Implementation notes:
- Use the pure `apply_task_outcome()` helper to update `completed/success/failed/consecutive_failures` consistently in both fixed and unlimited paths.
- Fixed batch paths (`run_batch_parallel`, `run_batch_pipeline`, and the Outlook wrapper that reuses them) should call the same `_finalize_batch_domain_stats()` before marking the batch finished.
- For unlimited mode, prefer `concurrency=1` in tests to avoid flaky completion ordering; production logic should still honor the configured concurrency.
- When auto-stop triggers, set `stop_reason="too_many_consecutive_failures"`, stop creating new tasks, and only mark `finished=True` after `await`-ing in-flight work.

- [ ] **Step 4: Re-run the focused runner tests**

Run: `pytest tests/test_registration_batch_routes.py -k "persists_email_address_even_on_failure or attaches_sorted_domain_stats or eleven_consecutive_failures" -v`

Expected: PASS with real email persistence, fixed-batch stats, and the unlimited auto-stop behavior working.

- [ ] **Step 5: Commit the batch execution changes**

```bash
git add src/web/routes/registration.py src/web/task_manager.py src/core/registration_batch_metrics.py tests/test_registration_batch_routes.py
git commit -m "feat: add unlimited registration runner and domain stats"
```

### Task 5: Update the registration page for unlimited mode and final stats rendering

**Files:**
- Modify: `templates/index.html:234-363`
- Modify: `static/js/app.js:429-706,884-911,1437-1500`
- Create: `tests_runtime/app_js_harness.py`
- Create: `tests/test_registration_page_assets.py`

- [ ] **Step 1: Write the failing template/app.js tests and harness scenarios**

```python
from pathlib import Path

from tests_runtime.app_js_harness import run_app_js_scenario


def test_registration_template_contains_unlimited_mode_and_domain_stats_container():
    template = Path("templates/index.html").read_text(encoding="utf-8")
    assert '<option value="unlimited">无限注册</option>' in template
    assert 'id="batch-consecutive-failures"' in template
    assert 'id="batch-domain-stats"' in template


def test_app_js_posts_count_zero_for_unlimited_mode():
    result = run_app_js_scenario("unlimited_mode_request")
    assert result["batch_count_display"] == "none"
    assert result["request_payload"]["count"] == 0
    assert result["saved_active_task"]["mode"] == "unlimited"


def test_app_js_renders_unlimited_progress_and_domain_stats():
    result = run_app_js_scenario("unlimited_progress")
    assert result["progress_text"] == "5/∞"
    assert result["progress_percent"] == "运行中"
    assert result["progress_bar_indeterminate"] is True
    assert result["consecutive_failures_text"] == "3/10"
    assert "gmail.com" in result["domain_stats_html"]
```

Harness notes for `tests_runtime/app_js_harness.py`:
- Mock DOM nodes for the registration form, batch progress section, and new domain-stats container.
- Mock `api.post()` so the scenario can inspect the outgoing request body.
- Export the functions you need to exercise (`handleModeChange`, `handleBatchRegistration`, `showBatchStatus`, `updateBatchProgress`).

- [ ] **Step 2: Run the new frontend tests and verify they fail**

Run: `pytest tests/test_registration_page_assets.py -v`

Expected: FAIL because the registration page does not yet expose the unlimited mode or the new rendering hooks/harness.

- [ ] **Step 3: Implement the template + app.js changes**

```javascript
function isUnlimitedRegistrationMode() {
  return elements.regMode.value === 'unlimited';
}

function handleModeChange(e) {
  const mode = e.target.value;
  isBatchMode = mode === 'batch' || mode === 'unlimited';
  elements.batchCountGroup.style.display = mode === 'batch' ? 'block' : 'none';
  elements.batchOptions.style.display = isBatchMode ? 'block' : 'none';
}

async function handleBatchRegistration(requestData) {
  const count = isUnlimitedRegistrationMode()
    ? 0
    : Math.min(500, Math.max(1, parseInt(elements.batchCount.value, 10) || 5));

  requestData.count = count;
  ...
  sessionStorage.setItem('activeTask', JSON.stringify({
    batch_id: data.batch_id,
    mode: isUnlimitedRegistrationMode() ? 'unlimited' : 'batch',
    total: data.count,
  }));
}

function updateBatchProgress(data) {
  if (data.is_unlimited) {
    elements.batchProgressText.textContent = `${data.completed}/∞`;
    elements.batchProgressPercent.textContent = data.finished ? '已结束' : '运行中';
    elements.progressBar.classList.add('indeterminate');
    elements.batchRemaining.textContent = '不限';
    elements.batchConsecutiveFailures.textContent = `${data.consecutive_failures}/${data.max_consecutive_failures}`;
    renderBatchDomainStats(data.domain_stats || []);
    return;
  }
  ...existing fixed-batch path...
}
```

Implementation notes:
- Keep the batch count input label at `注册数量 (1-500)` for normal batch mode only.
- Use the existing `.progress-bar.indeterminate` class; remove it when rendering fixed-count batches so percentages still work.
- `restoreActiveTask()` must recognize `mode === 'unlimited'` and reuse the same batch status endpoint.
- Domain stats should render only when `finished` is true and there is at least one stats row.
- If the stats table needs scroll, prefer a small wrapper div with `max-height` + `overflow-y:auto` in the template rather than adding broad new CSS.

- [ ] **Step 4: Re-run the frontend tests**

Run: `pytest tests/test_registration_page_assets.py -v`

Expected: PASS with the new mode, request payload, unlimited progress rendering, and final stats markup all covered.

- [ ] **Step 5: Commit the registration-page UI changes**

```bash
git add templates/index.html static/js/app.js tests_runtime/app_js_harness.py tests/test_registration_page_assets.py
git commit -m "feat: add unlimited registration UI and stats rendering"
```

### Task 6: Run end-to-end verification and land the final integration commit

**Files:**
- Modify: any files touched during polish from previous tasks
- Test: `tests/test_registration_batch_metrics.py`
- Test: `tests/test_registration_batch_routes.py`
- Test: `tests/test_registration_page_assets.py`
- Test: `tests/test_database_session.py`

- [ ] **Step 1: Run the targeted registration/unlimited suite**

Run: `pytest tests/test_registration_batch_metrics.py tests/test_registration_batch_routes.py tests/test_registration_page_assets.py tests/test_database_session.py -v`

Expected: PASS for the new helper, persistence, backend, and frontend coverage.

- [ ] **Step 2: Run nearby regression tests that touch registration/static assets**

Run: `pytest tests/test_registration_engine.py tests/test_email_service_duckmail_routes.py tests/test_static_asset_versioning.py -v`

Expected: PASS; no regressions to existing registration flow helpers or template asset references.

- [ ] **Step 3: Run the full suite if the targeted tests are green**

Run: `pytest -q`

Expected: PASS (or, if there are unrelated pre-existing failures, document them before proceeding instead of silently ignoring them).

- [ ] **Step 4: Inspect the final diff before committing**

Run: `git diff --stat`

Expected: only the planned registration/model/test files above should appear.

- [ ] **Step 5: Commit the integrated feature set**

```bash
git add src/database/models.py src/database/session.py src/database/crud.py src/core/registration_batch_metrics.py src/web/task_manager.py src/web/routes/registration.py templates/index.html static/js/app.js tests/test_database_session.py tests/test_registration_batch_metrics.py tests/test_registration_batch_routes.py tests/test_registration_page_assets.py tests_runtime/app_js_harness.py
git commit -m "feat: add unlimited registration mode and domain stats"
```
