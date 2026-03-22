# Scheduled Tasks Center Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an in-app scheduled task center for CPA cleanup, CPA refill, and aged-account token refresh, with persistent plans/runs, account invalidation tracking, and UI management.

**Architecture:** Add persistent scheduler data to the database, implement a small in-process scheduler engine with Asia/Shanghai trigger calculation, and isolate each maintenance workflow in its own runner. Expose the feature through dedicated API routes and a new “定时任务” page, while extending account APIs/UI to surface invalidation and CPA ownership data.

**Tech Stack:** FastAPI, SQLAlchemy, SQLite/PostgreSQL migrations, Jinja2 templates, vanilla JS, pytest, croniter, zoneinfo

---

## File Structure / Responsibility Map

### New files

- `src/scheduler/__init__.py` — package exports for scheduler services/engine
- `src/scheduler/schemas.py` — Pydantic request/response models for plans and runs
- `src/scheduler/time_utils.py` — Asia/Shanghai-aware cron and interval next-run calculation helpers
- `src/scheduler/service.py` — plan CRUD helpers, validation, next_run_at refresh, auto-disable helpers
- `src/scheduler/engine.py` — in-process scheduler loop, plan lock management, CPA lock management, dispatch entrypoints
- `src/scheduler/cpa_client.py` — reusable CPA remote count / invalid probe / delete helpers
- `src/scheduler/run_logger.py` — append-only run log buffer + summary finalization helpers
- `src/scheduler/runners/__init__.py` — runner exports
- `src/scheduler/runners/cleanup.py` — CPA cleanup runner
- `src/scheduler/runners/refill.py` — CPA refill runner and consecutive-failure auto-disable logic
- `src/scheduler/runners/refresh.py` — aged-account refresh runner
- `src/core/registration_job.py` — shared single-account registration helper reusable by web routes and refill runner
- `src/web/routes/scheduled_tasks.py` — scheduled plan/run API routes
- `templates/scheduled_tasks.html` — scheduled task page UI
- `static/js/scheduled_tasks.js` — scheduled task page behavior
- `tests/test_scheduler_data_model.py` — table/column migration + CRUD coverage for plans/runs
- `tests/test_scheduler_service.py` — trigger calculation and plan validation coverage
- `tests/test_scheduler_engine.py` — dispatch, overlap skip, startup idempotency coverage
- `tests/test_scheduler_cleanup_runner.py` — cleanup runner flow and local invalidation coverage
- `tests/test_scheduler_refill_runner.py` — refill success/failure/auto-disable coverage
- `tests/test_scheduler_refresh_runner.py` — refresh/invalidation/upload branch coverage
- `tests/test_scheduled_tasks_routes.py` — scheduled API coverage
- `tests/test_scheduled_tasks_page_assets.py` — page route, nav link, static asset coverage

### Modified files

- `pyproject.toml` — add `croniter`
- `requirements.txt` — add `croniter`
- `src/database/models.py` — extend `Account`; add `ScheduledPlan` and `ScheduledRun`
- `src/database/session.py` — add SQLite/PostgreSQL migrations for new account columns and scheduler tables
- `src/database/crud.py` — add CRUD helpers for scheduled plans/runs and account eligibility filters
- `src/web/app.py` — add scheduled tasks page route and scheduler startup/shutdown wiring
- `src/web/routes/__init__.py` — register scheduled task APIs
- `src/web/routes/accounts.py` — expose/filter invalidation and primary CPA fields
- `src/web/routes/registration.py` — switch single-account execution to `src/core/registration_job.py`
- `templates/index.html` — add nav link to scheduled tasks page
- `templates/accounts.html` — add columns/filters for invalidation + main CPA
- `templates/email_services.html` — add nav link
- `templates/payment.html` — add nav link
- `templates/settings.html` — add nav link
- `static/js/accounts.js` — load/render new account fields and filters
- `README.md` — document scheduled task center usage after implementation

---

### Task 1: Add scheduler persistence primitives and database coverage

**Files:**
- Modify: `pyproject.toml`
- Modify: `requirements.txt`
- Modify: `src/database/models.py`
- Modify: `src/database/session.py`
- Modify: `src/database/crud.py`
- Test: `tests/test_scheduler_data_model.py`
- Test: `tests/test_database_session.py`

- [ ] **Step 1: Write the failing table/column migration test**

```python
def test_scheduler_tables_and_account_columns_exist_after_init(tmp_path):
    manager = DatabaseSessionManager(f"sqlite:///{tmp_path / 'scheduler.db'}")
    Base.metadata.create_all(bind=manager.engine)
    manager.migrate_tables()

    inspector = inspect(manager.engine)
    assert "scheduled_plans" in inspector.get_table_names()
    assert "scheduled_runs" in inspector.get_table_names()

    account_columns = {col["name"] for col in inspector.get_columns("accounts")}
    assert {"primary_cpa_service_id", "invalidated_at", "invalid_reason"} <= account_columns
```

- [ ] **Step 2: Write the failing CRUD round-trip test for plans/runs**

```python
def test_create_plan_and_run_round_trip(temp_db):
    plan = crud.create_scheduled_plan(
        temp_db,
        name="nightly cleanup",
        task_type="cpa_cleanup",
        cpa_service_id=1,
        trigger_type="interval",
        interval_value=30,
        interval_unit="minutes",
        config={"max_cleanup_count": 20},
    )
    run = crud.create_scheduled_run(temp_db, plan_id=plan.id, trigger_source="manual")

    assert plan.config["max_cleanup_count"] == 20
    assert run.status == "running"
```

- [ ] **Step 3: Run the new data-layer tests and confirm they fail**

Run:
```bash
pytest tests/test_scheduler_data_model.py tests/test_database_session.py -q
```
Expected: FAIL because scheduler tables/columns/helpers do not exist yet.

- [ ] **Step 4: Add the dependency and ORM model changes**

```toml
# pyproject.toml
"croniter>=2.0.5",
```

```python
# src/database/models.py
class ScheduledPlan(Base):
    __tablename__ = "scheduled_plans"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(120), nullable=False)
    task_type = Column(String(32), nullable=False, index=True)
    enabled = Column(Boolean, default=True, nullable=False)
    cpa_service_id = Column(Integer, ForeignKey("cpa_services.id"), nullable=False, index=True)
    trigger_type = Column(String(16), nullable=False)
    cron_expression = Column(String(120))
    interval_value = Column(Integer)
    interval_unit = Column(String(16))
    config = Column(JSONEncodedDict, nullable=False)
    next_run_at = Column(DateTime)
    last_run_started_at = Column(DateTime)
    last_run_finished_at = Column(DateTime)
    last_run_status = Column(String(20))
    last_success_at = Column(DateTime)
    auto_disabled_reason = Column(String(120))
```

```python
# src/database/models.py
primary_cpa_service_id = Column(Integer)
invalidated_at = Column(DateTime)
invalid_reason = Column(String(64))
```

- [ ] **Step 5: Add migration entries and CRUD helpers**

```python
# src/database/session.py
SQLITE_MIGRATIONS.extend([
    ("accounts", "primary_cpa_service_id", "INTEGER"),
    ("accounts", "invalidated_at", "DATETIME"),
    ("accounts", "invalid_reason", "VARCHAR(64)"),
])
```

```python
# src/database/crud.py
def create_scheduled_plan(...): ...
def get_scheduled_plans(...): ...
def create_scheduled_run(...): ...
def append_scheduled_run_log(...): ...
def finish_scheduled_run(...): ...
```

- [ ] **Step 6: Re-run the focused database tests**

Run:
```bash
pytest tests/test_scheduler_data_model.py tests/test_database_session.py -q
```
Expected: PASS.

- [ ] **Step 7: Commit the persistence layer**

```bash
git add pyproject.toml requirements.txt src/database/models.py src/database/session.py src/database/crud.py tests/test_scheduler_data_model.py tests/test_database_session.py
git commit -m "feat: add scheduler persistence models"
```

---

### Task 2: Build trigger calculation and plan validation services

**Files:**
- Create: `src/scheduler/__init__.py`
- Create: `src/scheduler/schemas.py`
- Create: `src/scheduler/time_utils.py`
- Create: `src/scheduler/service.py`
- Test: `tests/test_scheduler_service.py`

- [ ] **Step 1: Write failing tests for Asia/Shanghai trigger calculation**

```python
def test_compute_next_run_at_for_cron_uses_asia_shanghai():
    now = datetime(2026, 3, 22, 8, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
    plan = SimpleNamespace(trigger_type="cron", cron_expression="0 9 * * *")

    result = compute_next_run_at(plan, now=now)

    assert result == datetime(2026, 3, 22, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_compute_next_run_at_for_interval_adds_hours():
    now = datetime(2026, 3, 22, 8, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
    plan = SimpleNamespace(trigger_type="interval", interval_value=2, interval_unit="hours")
    assert compute_next_run_at(plan, now=now) == datetime(2026, 3, 22, 10, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
```

- [ ] **Step 2: Write failing tests for plan payload validation**

```python
def test_validate_refill_config_requires_failure_threshold():
    with pytest.raises(ValueError, match="max_consecutive_failures"):
        validate_plan_payload(
            task_type="cpa_refill",
            trigger_type="interval",
            config={"target_valid_count": 50, "max_refill_count": 10},
        )
```

- [ ] **Step 3: Run the service tests and confirm they fail**

Run:
```bash
pytest tests/test_scheduler_service.py -q
```
Expected: FAIL because scheduler service/time helpers are missing.

- [ ] **Step 4: Implement the time and validation helpers**

```python
# src/scheduler/time_utils.py
SCHEDULER_TZ = ZoneInfo("Asia/Shanghai")


def compute_next_run_at(plan, now=None):
    now = (now or datetime.now(SCHEDULER_TZ)).astimezone(SCHEDULER_TZ)
    if plan.trigger_type == "cron":
        return croniter(plan.cron_expression, now).get_next(datetime).astimezone(SCHEDULER_TZ)
    delta = timedelta(**{plan.interval_unit: plan.interval_value})
    return now + delta
```

```python
# src/scheduler/service.py
def validate_plan_payload(...):
    if task_type == "cpa_refill" and not config.get("max_consecutive_failures"):
        raise ValueError("max_consecutive_failures is required")
```

- [ ] **Step 5: Add Pydantic schemas for route payloads and responses**

```python
class ScheduledPlanCreate(BaseModel):
    name: str
    task_type: Literal["cpa_cleanup", "cpa_refill", "account_refresh"]
    cpa_service_id: int
    trigger_type: Literal["cron", "interval"]
    cron_expression: str | None = None
    interval_value: int | None = None
    interval_unit: Literal["minutes", "hours"] | None = None
    config: dict[str, Any]
    enabled: bool = True
```

- [ ] **Step 6: Re-run the scheduler service tests**

Run:
```bash
pytest tests/test_scheduler_service.py -q
```
Expected: PASS.

- [ ] **Step 7: Commit the trigger/service layer**

```bash
git add src/scheduler/__init__.py src/scheduler/schemas.py src/scheduler/time_utils.py src/scheduler/service.py tests/test_scheduler_service.py
git commit -m "feat: add scheduler trigger and validation services"
```

---

### Task 3: Add the scheduler engine and app lifecycle wiring

**Files:**
- Create: `src/scheduler/engine.py`
- Create: `src/scheduler/run_logger.py`
- Modify: `src/web/app.py`
- Test: `tests/test_scheduler_engine.py`

- [ ] **Step 1: Write failing tests for engine overlap skipping and startup idempotency**

```python
def test_scheduler_engine_skips_plan_when_same_plan_is_running(monkeypatch):
    repo = FakeRepo(due_plan_ids=[1])
    engine = SchedulerEngine(repo=repo)
    engine._plan_locks.add(1)

    engine.dispatch_due_plans_once()

    assert repo.created_runs == [(1, "scheduled", "skipped")]


def test_scheduler_engine_start_is_idempotent():
    engine = SchedulerEngine(repo=FakeRepo())
    assert engine.start() is True
    assert engine.start() is False
```

- [ ] **Step 2: Run the engine tests and confirm they fail**

Run:
```bash
pytest tests/test_scheduler_engine.py -q
```
Expected: FAIL because `SchedulerEngine` is not implemented.

- [ ] **Step 3: Implement minimal engine behavior and run logging helpers**

```python
class SchedulerEngine:
    def __init__(self, repo=None, poll_seconds=15):
        self.repo = repo or SchedulerRepository()
        self.poll_seconds = poll_seconds
        self._started = False
        self._plan_locks: set[int] = set()
        self._cpa_locks: set[int] = set()
```

```python
def dispatch_due_plans_once(self):
    for plan in self.repo.get_due_enabled_plans(now_in_scheduler_tz()):
        if plan.id in self._plan_locks:
            self.repo.create_skipped_run(plan.id, trigger_source="scheduled", reason="plan already running")
            continue
```

- [ ] **Step 4: Wire the engine into FastAPI startup/shutdown**

```python
# src/web/app.py
from ..scheduler.engine import scheduler_engine

@app.on_event("startup")
async def startup_event():
    ...
    scheduler_engine.start()

@app.on_event("shutdown")
async def shutdown_event():
    scheduler_engine.stop()
```

- [ ] **Step 5: Re-run the engine tests**

Run:
```bash
pytest tests/test_scheduler_engine.py -q
```
Expected: PASS.

- [ ] **Step 6: Commit the engine wiring**

```bash
git add src/scheduler/engine.py src/scheduler/run_logger.py src/web/app.py tests/test_scheduler_engine.py
git commit -m "feat: add in-process scheduler engine"
```

---

### Task 4: Implement the CPA cleanup runner and remote maintenance client

**Files:**
- Create: `src/scheduler/cpa_client.py`
- Create: `src/scheduler/runners/__init__.py`
- Create: `src/scheduler/runners/cleanup.py`
- Modify: `src/database/crud.py`
- Test: `tests/test_scheduler_cleanup_runner.py`

- [ ] **Step 1: Write the failing cleanup runner tests**

```python
def test_cleanup_runner_marks_local_accounts_expired_for_matching_primary_cpa(temp_db, monkeypatch):
    account = crud.create_account(temp_db, email="a@example.com", email_service="tempmail")
    crud.update_account(temp_db, account.id, primary_cpa_service_id=3, status="active")

    monkeypatch.setattr(cleanup_runner, "probe_invalid_accounts", lambda **_: [{"email": "a@example.com", "name": "a@example.com.json"}])
    monkeypatch.setattr(cleanup_runner, "delete_invalid_accounts", lambda **_: {"deleted": 1, "failed": 0})

    summary = run_cleanup_plan(plan_id=1, run_id=1)

    refreshed = crud.get_account_by_id(temp_db, account.id)
    assert refreshed.status == "expired"
    assert refreshed.invalid_reason == "cpa_cleanup"
    assert summary["local_marked_expired"] == 1
```

- [ ] **Step 2: Run cleanup tests and confirm they fail**

Run:
```bash
pytest tests/test_scheduler_cleanup_runner.py -q
```
Expected: FAIL because CPA client / cleanup runner functions are missing.

- [ ] **Step 3: Implement the CPA remote helper surface**

```python
# src/scheduler/cpa_client.py
def count_valid_accounts(service, *, timeout=20) -> int: ...
def probe_invalid_accounts(service, *, limit=None, timeout=20) -> list[dict[str, Any]]: ...
def delete_invalid_accounts(service, names: list[str], *, timeout=20) -> dict[str, int]: ...
```

- [ ] **Step 4: Implement the cleanup runner with run logging**

```python
# src/scheduler/runners/cleanup.py
for item in invalid_items[:max_cleanup_count]:
    crud.mark_account_expired_by_email_and_cpa(
        db,
        email=item["email"],
        cpa_service_id=plan.cpa_service_id,
        reason="cpa_cleanup",
    )
```

- [ ] **Step 5: Re-run cleanup tests**

Run:
```bash
pytest tests/test_scheduler_cleanup_runner.py -q
```
Expected: PASS.

- [ ] **Step 6: Commit the cleanup workflow**

```bash
git add src/scheduler/cpa_client.py src/scheduler/runners/__init__.py src/scheduler/runners/cleanup.py src/database/crud.py tests/test_scheduler_cleanup_runner.py
git commit -m "feat: add scheduled CPA cleanup runner"
```

---

### Task 5: Extract reusable registration job logic and implement the refill runner

**Files:**
- Create: `src/core/registration_job.py`
- Create: `src/scheduler/runners/refill.py`
- Modify: `src/web/routes/registration.py`
- Modify: `src/database/crud.py`
- Test: `tests/test_scheduler_refill_runner.py`
- Test: `tests/test_registration_batch_routes.py`

- [ ] **Step 1: Write failing tests for refill success and auto-disable behavior**

```python
def test_refill_runner_requires_cpa_upload_for_success(temp_db, monkeypatch):
    monkeypatch.setattr(refill_runner, "count_valid_accounts", lambda *a, **k: 2)
    monkeypatch.setattr(refill_runner, "run_registration_job", lambda **_: RegistrationJobResult(success=True, account_id=11))
    monkeypatch.setattr(refill_runner, "upload_account_to_bound_cpa", lambda **_: (True, "ok"))

    summary = run_refill_plan(plan_id=1, run_id=1)
    assert summary["uploaded_success"] == 1


def test_refill_runner_auto_disables_plan_after_consecutive_failures(temp_db, monkeypatch):
    monkeypatch.setattr(refill_runner, "count_valid_accounts", lambda *a, **k: 0)
    monkeypatch.setattr(refill_runner, "run_registration_job", lambda **_: RegistrationJobResult(success=False, error_message="boom"))

    summary = run_refill_plan(plan_id=1, run_id=1)
    plan = crud.get_scheduled_plan_by_id(temp_db, 1)
    assert plan.enabled is False
    assert plan.auto_disabled_reason == "consecutive_failures_reached"
    assert summary["auto_disabled"] is True
```

- [ ] **Step 2: Run the refill and registration regression tests**

Run:
```bash
pytest tests/test_scheduler_refill_runner.py tests/test_registration_batch_routes.py -q
```
Expected: FAIL because the refill runner and shared registration helper do not exist yet.

- [ ] **Step 3: Extract one-account registration into a shared helper**

```python
# src/core/registration_job.py
@dataclass
class RegistrationJobResult:
    success: bool
    account_id: int | None = None
    email: str | None = None
    error_message: str | None = None


def run_registration_job(*, db, email_service_type, email_service_id, proxy, email_service_config, auto_upload=False):
    ...
```

- [ ] **Step 4: Update `registration.py` to call the shared helper instead of duplicating the flow**

```python
result = run_registration_job(
    db=db,
    email_service_type=email_service_type,
    email_service_id=email_service_id,
    proxy=actual_proxy_url,
    email_service_config=config,
)
```

- [ ] **Step 5: Implement refill runner loop with upload-required success criteria**

```python
while uploaded_success < refill_target:
    job = run_registration_job(...)
    if not job.success:
        consecutive_failures += 1
        if consecutive_failures >= max_failures:
            disable_plan(...)
            break
        continue

    ok, msg = upload_account_to_bound_cpa(db=db, account_id=job.account_id, cpa_service_id=plan.cpa_service_id)
    if ok:
        consecutive_failures = 0
        uploaded_success += 1
    else:
        consecutive_failures += 1
```

- [ ] **Step 6: Re-run the refill and registration tests**

Run:
```bash
pytest tests/test_scheduler_refill_runner.py tests/test_registration_batch_routes.py -q
```
Expected: PASS.

- [ ] **Step 7: Commit the refill workflow**

```bash
git add src/core/registration_job.py src/scheduler/runners/refill.py src/web/routes/registration.py src/database/crud.py tests/test_scheduler_refill_runner.py tests/test_registration_batch_routes.py
git commit -m "feat: add scheduled CPA refill runner"
```

---

### Task 6: Implement the aged-account refresh runner

**Files:**
- Create: `src/scheduler/runners/refresh.py`
- Modify: `src/database/crud.py`
- Test: `tests/test_scheduler_refresh_runner.py`

- [ ] **Step 1: Write failing tests for refresh selection and invalidation branches**

```python
def test_refresh_runner_selects_due_accounts_from_registered_at_when_last_refresh_missing(temp_db):
    account = make_account(temp_db, status="active", primary_cpa_service_id=5, registered_days_ago=8, last_refresh=None)
    eligible = crud.get_due_refresh_accounts(temp_db, cpa_service_id=5, refresh_after_days=7, limit=10)
    assert [item.id for item in eligible] == [account.id]


def test_refresh_runner_marks_account_expired_on_refresh_failure(temp_db, monkeypatch):
    monkeypatch.setattr(refresh_runner, "refresh_account_token", lambda *a, **k: SimpleNamespace(success=False, error_message="bad refresh"))
    summary = run_refresh_plan(plan_id=1, run_id=1)
    account = crud.get_account_by_id(temp_db, 1)
    assert account.status == "expired"
    assert account.invalid_reason == "refresh_failed"


def test_refresh_runner_keeps_account_active_when_cpa_upload_fails(temp_db, monkeypatch):
    monkeypatch.setattr(refresh_runner, "refresh_account_token", lambda *a, **k: SimpleNamespace(success=True, access_token="ak", refresh_token="rk", expires_at=datetime.utcnow()))
    monkeypatch.setattr(refresh_runner, "check_subscription_status", lambda *a, **k: "plus")
    monkeypatch.setattr(refresh_runner, "upload_account_to_bound_cpa", lambda **_: (False, "upstream down"))
    run_refresh_plan(plan_id=1, run_id=1)
    account = crud.get_account_by_id(temp_db, 1)
    assert account.status == "active"
    assert account.cpa_uploaded is False
```

- [ ] **Step 2: Run refresh tests and confirm they fail**

Run:
```bash
pytest tests/test_scheduler_refresh_runner.py -q
```
Expected: FAIL because refresh runner helpers are missing.

- [ ] **Step 3: Implement account selection and invalidation helpers in CRUD**

```python
# src/database/crud.py
def get_due_refresh_accounts(db, *, cpa_service_id, refresh_after_days, limit): ...
def mark_account_expired(db, account_id, reason): ...
def mark_account_active_for_cpa(db, account_id, cpa_service_id): ...
```

- [ ] **Step 4: Implement refresh runner branch handling**

```python
refresh_result = refresh_account_token(account.id, proxy_url=proxy)
if not refresh_result.success:
    crud.mark_account_expired(db, account.id, reason="refresh_failed")
    continue

subscription = check_subscription_status(account, proxy)
if subscription_error:
    crud.mark_account_expired(db, account.id, reason="subscription_check_failed")
    continue
```

- [ ] **Step 5: Re-run refresh tests**

Run:
```bash
pytest tests/test_scheduler_refresh_runner.py -q
```
Expected: PASS.

- [ ] **Step 6: Commit the refresh workflow**

```bash
git add src/scheduler/runners/refresh.py src/database/crud.py tests/test_scheduler_refresh_runner.py
git commit -m "feat: add scheduled account refresh runner"
```

---

### Task 7: Add scheduled task APIs and account API enhancements

**Files:**
- Create: `src/web/routes/scheduled_tasks.py`
- Modify: `src/web/routes/__init__.py`
- Modify: `src/web/routes/accounts.py`
- Test: `tests/test_scheduled_tasks_routes.py`

- [ ] **Step 1: Write failing route tests for plan CRUD and manual run actions**

```python
def test_create_scheduled_plan_route_persists_next_run_at(client):
    response = client.post(
        "/api/scheduled-plans",
        json={
            "name": "morning refill",
            "task_type": "cpa_refill",
            "cpa_service_id": 2,
            "trigger_type": "cron",
            "cron_expression": "0 8 * * *",
            "config": {"target_valid_count": 50, "max_refill_count": 10, "max_consecutive_failures": 3, "registration_profile": {}},
            "enabled": True,
        },
    )
    assert response.status_code == 200
    assert response.json()["next_run_at"].startswith("2026-")


def test_manual_run_route_rejects_when_plan_is_currently_running(client, monkeypatch):
    monkeypatch.setattr(scheduled_routes.scheduler_engine, "trigger_plan_now", lambda plan_id: False)
    response = client.post("/api/scheduled-plans/1/run")
    assert response.status_code == 409
```

- [ ] **Step 2: Write a failing account filter/response test**

```python
def test_list_accounts_can_filter_by_primary_cpa_and_returns_invalid_fields(client):
    response = client.get("/api/accounts", params={"status": "expired", "primary_cpa_service_id": 7})
    body = response.json()
    assert body["accounts"][0]["primary_cpa_service_id"] == 7
    assert "invalidated_at" in body["accounts"][0]
    assert "invalid_reason" in body["accounts"][0]
```

- [ ] **Step 3: Run the API tests and confirm they fail**

Run:
```bash
pytest tests/test_scheduled_tasks_routes.py -q
```
Expected: FAIL because scheduled task routes and account filters are incomplete.

- [ ] **Step 4: Implement the route module and register it**

```python
# src/web/routes/scheduled_tasks.py
@router.post("")
async def create_scheduled_plan(request: ScheduledPlanCreate): ...

@router.post("/{plan_id}/run")
async def run_scheduled_plan(plan_id: int): ...
```

```python
# src/web/routes/__init__.py
from .scheduled_tasks import router as scheduled_tasks_router
api_router.include_router(scheduled_tasks_router, prefix="/scheduled-plans", tags=["scheduled-plans"])
```

- [ ] **Step 5: Extend account list/filter/response fields**

```python
# src/web/routes/accounts.py
primary_cpa_service_id: Optional[int] = None
invalidated_at: Optional[str] = None
invalid_reason: Optional[str] = None
```

```python
if primary_cpa_service_id:
    query = query.filter(Account.primary_cpa_service_id == primary_cpa_service_id)
```

- [ ] **Step 6: Re-run route tests**

Run:
```bash
pytest tests/test_scheduled_tasks_routes.py -q
```
Expected: PASS.

- [ ] **Step 7: Commit the API layer**

```bash
git add src/web/routes/scheduled_tasks.py src/web/routes/__init__.py src/web/routes/accounts.py tests/test_scheduled_tasks_routes.py
git commit -m "feat: add scheduled task APIs"
```

---

### Task 8: Add the scheduled task page, navigation, and account page UI updates

**Files:**
- Create: `templates/scheduled_tasks.html`
- Create: `static/js/scheduled_tasks.js`
- Modify: `src/web/app.py`
- Modify: `templates/index.html`
- Modify: `templates/accounts.html`
- Modify: `templates/email_services.html`
- Modify: `templates/payment.html`
- Modify: `templates/settings.html`
- Modify: `static/js/accounts.js`
- Test: `tests/test_scheduled_tasks_page_assets.py`
- Test: `tests/test_registration_page_assets.py`

- [ ] **Step 1: Write failing asset/page tests**

```python
def test_scheduled_tasks_page_requires_auth_and_renders_script(client):
    response = client.get("/scheduled-tasks")
    assert response.status_code in {200, 302}


def test_nav_contains_scheduled_tasks_link(client):
    response = client.get("/")
    assert '/scheduled-tasks' in response.text
```

- [ ] **Step 2: Run the page asset tests and confirm they fail**

Run:
```bash
pytest tests/test_scheduled_tasks_page_assets.py tests/test_registration_page_assets.py -q
```
Expected: FAIL because the new page/links/assets are missing.

- [ ] **Step 3: Add the page route and template skeleton**

```python
# src/web/app.py
@app.get("/scheduled-tasks", response_class=HTMLResponse)
async def scheduled_tasks_page(request: Request):
    if not _is_authenticated(request):
        return _redirect_to_login(request)
    return templates.TemplateResponse("scheduled_tasks.html", {"request": request})
```

```html
<!-- templates/scheduled_tasks.html -->
<a href="/scheduled-tasks" class="nav-link active">定时任务</a>
<table id="scheduled-plans-table"></table>
<div id="plan-modal"></div>
<div id="run-log-modal"></div>
<script src="/static/js/scheduled_tasks.js?v={{ static_version }}"></script>
```

- [ ] **Step 4: Implement the page JS and account table rendering changes**

```javascript
// static/js/scheduled_tasks.js
async function loadPlans() {
  const data = await api.get('/scheduled-plans');
  renderPlans(data.plans || data);
}
```

```javascript
// static/js/accounts.js
currentFilters = { status: '', email_service: '', search: '', primary_cpa_service_id: '' };
```

- [ ] **Step 5: Update all authenticated nav templates**

```html
<a href="/scheduled-tasks" class="nav-link">定时任务</a>
```

- [ ] **Step 6: Re-run the page asset tests**

Run:
```bash
pytest tests/test_scheduled_tasks_page_assets.py tests/test_registration_page_assets.py -q
```
Expected: PASS.

- [ ] **Step 7: Commit the UI layer**

```bash
git add src/web/app.py templates/scheduled_tasks.html static/js/scheduled_tasks.js templates/index.html templates/accounts.html templates/email_services.html templates/payment.html templates/settings.html static/js/accounts.js tests/test_scheduled_tasks_page_assets.py tests/test_registration_page_assets.py
git commit -m "feat: add scheduled tasks UI"
```

---

### Task 9: Document the feature and run full verification

**Files:**
- Modify: `README.md`
- Test: `tests/test_scheduler_data_model.py`
- Test: `tests/test_scheduler_service.py`
- Test: `tests/test_scheduler_engine.py`
- Test: `tests/test_scheduler_cleanup_runner.py`
- Test: `tests/test_scheduler_refill_runner.py`
- Test: `tests/test_scheduler_refresh_runner.py`
- Test: `tests/test_scheduled_tasks_routes.py`
- Test: `tests/test_scheduled_tasks_page_assets.py`
- Test: `tests/test_registration_batch_routes.py`
- Test: `tests/test_registration_page_assets.py`

- [ ] **Step 1: Update README usage docs for scheduled tasks**

```markdown
## 定时任务

- 支持 CPA 清理、CPA 补号、老账号刷新
- 计划触发支持 Cron 和固定频率
- Cron 时区固定为 Asia/Shanghai
- 补号任务支持连续失败自动禁用
```

- [ ] **Step 2: Run the focused scheduler/API/UI regression suite**

Run:
```bash
pytest \
  tests/test_scheduler_data_model.py \
  tests/test_scheduler_service.py \
  tests/test_scheduler_engine.py \
  tests/test_scheduler_cleanup_runner.py \
  tests/test_scheduler_refill_runner.py \
  tests/test_scheduler_refresh_runner.py \
  tests/test_scheduled_tasks_routes.py \
  tests/test_scheduled_tasks_page_assets.py \
  tests/test_registration_batch_routes.py \
  tests/test_registration_page_assets.py -q
```
Expected: PASS.

- [ ] **Step 3: Run the broader project test suite**

Run:
```bash
pytest -q
```
Expected: PASS.

- [ ] **Step 4: Manual smoke-check the UI in a browser**

Run:
```bash
python webui.py --debug
```
Expected:
- `/scheduled-tasks` page loads after login
- Create/edit/enable/disable/run actions work
- Accounts page can filter expired accounts and show invalidation fields
- Scheduler starts once and logs plan execution without duplicate loops

- [ ] **Step 5: Commit documentation and final polish**

```bash
git add README.md
git commit -m "docs: add scheduled tasks usage"
```

---

## Local review checklist for the implementer

- [ ] `scheduled_plans` / `scheduled_runs` are created on fresh DBs and migrated on existing DBs
- [ ] Account invalidation updates never delete rows automatically
- [ ] Refill success requires CPA upload success
- [ ] Refill run auto-disables the plan after the configured consecutive failure threshold
- [ ] Refresh run expires accounts only on refresh/subscription failure, not on CPA upload failure
- [ ] Same plan overlap yields a persisted `skipped` run
- [ ] Scheduler startup is idempotent under `--debug` reload scenarios
- [ ] Authenticated navigation includes `/scheduled-tasks` on all main pages

