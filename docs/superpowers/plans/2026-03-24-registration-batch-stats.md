# Registration Batch Stats Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist final statistics for ordinary batch registrations, expose list/detail/A-B comparison APIs, and add a dedicated Web UI page for browsing and comparing batch statistics.

**Architecture:** Add a dedicated batch-statistics domain parallel to the existing experiment domain. Ordinary batch execution in `src/web/routes/registration.py` will capture batch context and, on terminal completion (`completed / failed / cancelled`), call a new aggregation module that reads `registration_tasks` and `pipeline_step_runs`, writes a snapshot into new statistics tables, and powers list/detail/compare APIs plus a standalone `/registration-batch-stats` page.

**Tech Stack:** FastAPI, SQLAlchemy ORM, existing SQLite/PostgreSQL support, Jinja2 templates, vanilla JS, pytest, existing JS harness pattern in `tests_runtime/`.

---

## File Map

### Create

- `src/core/registration_batch_stats.py` — batch statistics aggregation, stage mapping, finalization, and A/B diff builders.
- `src/web/routes/registration_batch_stats.py` — list/detail/compare APIs for batch statistics.
- `templates/registration_batch_stats.html` — standalone page for batch statistics list/detail/comparison.
- `static/js/registration_batch_stats.js` — client logic for list loading, row selection, detail rendering, and A/B compare rendering.
- `tests/test_registration_batch_stats_models.py` — ORM persistence tests for new tables.
- `tests/test_registration_batch_stats.py` — aggregation/finalization unit tests.
- `tests/test_registration_batch_stats_routes.py` — API route tests.
- `tests/test_registration_batch_stats_page_assets.py` — page asset and route tests.
- `tests_runtime/registration_batch_stats_js_harness.py` — JS runtime harness for the new page.

### Modify

- `src/database/models.py` — add `RegistrationBatchStat`, `RegistrationBatchStepStat`, `RegistrationBatchStageStat`.
- `src/database/crud.py` — CRUD helpers for new batch-statistics models.
- `src/web/routes/registration.py` — record batch context, finalize stats on ordinary batch terminal states.
- `src/web/routes/__init__.py` — register new router.
- `src/web/app.py` — add authenticated page route.
- `static/css/style.css` — add styles for batch statistics list/detail/compare layouts.
- `README.md` — optional short feature note if page navigation/feature summary is updated.

---

### Task 1: Add ORM models and persistence helpers for batch statistics

**Files:**
- Modify: `src/database/models.py`
- Modify: `src/database/crud.py`
- Test: `tests/test_registration_batch_stats_models.py`

- [ ] **Step 1: Write the failing model persistence test**

```python
def test_create_registration_batch_stat_persists_children(temp_db):
    batch = crud.create_registration_batch_stat(
        temp_db,
        batch_id="batch-001",
        status="completed",
        mode="pipeline",
        pipeline_key="current_pipeline",
        target_count=5,
        finished_count=5,
        success_count=4,
        failed_count=1,
        total_duration_ms=50000,
        avg_duration_ms=12500.0,
    )
    crud.create_registration_batch_step_stat(
        temp_db,
        batch_stat_id=batch.id,
        step_key="create_email",
        step_order=1,
        sample_count=4,
        success_count=4,
        avg_duration_ms=120.0,
        p50_duration_ms=110,
        p90_duration_ms=150,
    )
    crud.create_registration_batch_stage_stat(
        temp_db,
        batch_stat_id=batch.id,
        stage_key="signup_prepare",
        sample_count=4,
        avg_duration_ms=450.0,
        p50_duration_ms=420,
        p90_duration_ms=520,
    )

    assert batch.id is not None
    assert batch.step_stats[0].step_key == "create_email"
    assert batch.stage_stats[0].stage_key == "signup_prepare"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_registration_batch_stats_models.py -v`

Expected: FAIL because the batch statistics ORM models and CRUD helpers do not exist yet.

- [ ] **Step 3: Add the new ORM models**

```python
class RegistrationBatchStat(Base):
    __tablename__ = "registration_batch_stats"

    id = Column(Integer, primary_key=True, autoincrement=True)
    batch_id = Column(String(36), nullable=False, unique=True, index=True)
    status = Column(String(20), nullable=False, index=True)
    mode = Column(String(32), nullable=False)
    pipeline_key = Column(String(64), nullable=False, index=True)
    email_service_type = Column(String(50))
    email_service_id = Column(Integer, ForeignKey("email_services.id"), index=True)
    proxy_strategy_snapshot = Column(JSONEncodedDict)
    config_snapshot = Column(JSONEncodedDict)
    target_count = Column(Integer, default=0)
    finished_count = Column(Integer, default=0)
    success_count = Column(Integer, default=0)
    failed_count = Column(Integer, default=0)
    total_duration_ms = Column(Integer)
    avg_duration_ms = Column(Integer)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)

    step_stats = relationship("RegistrationBatchStepStat", back_populates="batch_stat", cascade="all, delete-orphan")
    stage_stats = relationship("RegistrationBatchStageStat", back_populates="batch_stat", cascade="all, delete-orphan")
```

- [ ] **Step 4: Add step/stage child models and CRUD helpers**

```python
def create_registration_batch_step_stat(db: Session, **kwargs) -> RegistrationBatchStepStat:
    row = RegistrationBatchStepStat(**kwargs)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def create_registration_batch_stage_stat(db: Session, **kwargs) -> RegistrationBatchStageStat:
    row = RegistrationBatchStageStat(**kwargs)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row
```

- [ ] **Step 5: Run the persistence test to verify it passes**

Run: `uv run pytest tests/test_registration_batch_stats_models.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/database/models.py src/database/crud.py tests/test_registration_batch_stats_models.py
git commit -m "feat: add registration batch stats models"
```

---

### Task 2: Build aggregation/finalization core for batch statistics

**Files:**
- Create: `src/core/registration_batch_stats.py`
- Modify: `src/database/crud.py`
- Test: `tests/test_registration_batch_stats.py`

- [ ] **Step 1: Write the failing aggregation tests**

```python
def test_finalize_batch_statistics_builds_snapshot_for_completed_batch(temp_db):
    stat = finalize_batch_statistics(
        temp_db,
        batch_context={
            "batch_id": "batch-001",
            "status": "completed",
            "mode": "pipeline",
            "pipeline_key": "current_pipeline",
            "target_count": 2,
            "task_uuids": ["task-1", "task-2"],
            "started_at": start_at,
            "completed_at": end_at,
        },
    )

    assert stat.success_count == 1
    assert stat.failed_count == 1
    assert any(item.step_key == "create_email" for item in stat.step_stats)
    assert any(item.stage_key == "signup_prepare" for item in stat.stage_stats)
```

```python
def test_finalize_batch_statistics_keeps_cancelled_batch_snapshot(temp_db):
    stat = finalize_batch_statistics(temp_db, batch_context={... "status": "cancelled", ...})
    assert stat.status == "cancelled"
    assert stat.finished_count == 1
```

```python
def test_build_batch_stats_compare_aligns_missing_steps_and_stages(temp_db):
    compare = build_batch_stats_compare(left_stat, right_stat)
    assert compare["step_diffs"][0]["left"]["avg_duration_ms"] is not None
    assert compare["step_diffs"][0]["right"]["avg_duration_ms"] is None
```

- [ ] **Step 2: Run the aggregation tests to verify they fail**

Run: `uv run pytest tests/test_registration_batch_stats.py -v`

Expected: FAIL because the aggregation/finalization module does not exist yet.

- [ ] **Step 3: Implement stage mapping and summary builders**

```python
STEP_STAGE_MAP = {
    "get_proxy_ip": "signup_prepare",
    "create_email": "signup_prepare",
    "init_signup_session": "signup_prepare",
    "send_signup_otp": "signup_otp",
    "wait_signup_otp": "signup_otp",
    "validate_signup_otp": "signup_otp",
    "create_account": "create_account",
    "init_login_session": "login_prepare",
    "submit_login_email": "login_prepare",
    "submit_login_password": "login_prepare",
    "wait_login_otp": "login_otp",
    "validate_login_otp": "login_otp",
    "select_workspace": "token_exchange",
    "exchange_oauth_token": "token_exchange",
}
```

- [ ] **Step 4: Implement `finalize_batch_statistics(...)` with idempotency**

```python
def finalize_batch_statistics(db: Session, *, batch_context: dict[str, Any]) -> RegistrationBatchStat:
    existing = crud.get_registration_batch_stat_by_batch_id(db, batch_context["batch_id"])
    if existing is not None:
        return existing

    tasks = crud.get_registration_tasks_by_uuids(db, batch_context["task_uuids"])
    step_rows = crud.get_pipeline_step_runs_by_task_uuids(db, batch_context["task_uuids"])
    # build summary, step stats, stage stats, then persist snapshot
```

- [ ] **Step 5: Implement compare builder**

```python
def build_batch_stats_compare(left: RegistrationBatchStat, right: RegistrationBatchStat) -> dict[str, Any]:
    return {
        "left": _batch_stat_to_dict(left),
        "right": _batch_stat_to_dict(right),
        "summary_diff": {...},
        "step_diffs": _diff_step_stats(left.step_stats, right.step_stats),
        "stage_diffs": _diff_stage_stats(left.stage_stats, right.stage_stats),
    }
```

- [ ] **Step 6: Run the aggregation tests to verify they pass**

Run: `uv run pytest tests/test_registration_batch_stats.py -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/core/registration_batch_stats.py src/database/crud.py tests/test_registration_batch_stats.py
git commit -m "feat: add batch statistics aggregation"
```

---

### Task 3: Integrate statistics finalization into ordinary batch execution

**Files:**
- Modify: `src/web/routes/registration.py`
- Test: `tests/test_registration_batch_routes.py`

- [ ] **Step 1: Write the failing integration tests**

```python
def test_run_batch_parallel_finalizes_statistics_on_completed_batch(route_db, monkeypatch):
    observed = {}

    def fake_finalize(db, *, batch_context):
        observed["status"] = batch_context["status"]
        observed["pipeline_key"] = batch_context["pipeline_key"]
        observed["target_count"] = batch_context["target_count"]

    monkeypatch.setattr(registration_routes, "finalize_batch_statistics", fake_finalize)
    # run batch
    assert observed == {
        "status": "completed",
        "pipeline_key": "current_pipeline",
        "target_count": 2,
    }
```

```python
def test_run_batch_pipeline_finalizes_statistics_on_cancelled_batch(route_db, monkeypatch):
    observed = {}
    monkeypatch.setattr(registration_routes, "finalize_batch_statistics", lambda db, *, batch_context: observed.update(batch_context))
    # cancel pipeline batch
    assert observed["status"] == "cancelled"
```

- [ ] **Step 2: Run the focused registration batch tests and verify failure**

Run: `uv run pytest tests/test_registration_batch_routes.py -k "finalizes_statistics" -v`

Expected: FAIL because ordinary batch execution does not finalize statistics yet.

- [ ] **Step 3: Extend batch runtime state with statistics context**

```python
batch_tasks[batch_id] = {
    ...,
    "pipeline_key": pipeline_key,
    "email_service_type": email_service_type,
    "email_service_id": email_service_id,
    "config_snapshot": {
        "proxy": proxy,
        "interval_min": interval_min,
        "interval_max": interval_max,
        "concurrency": concurrency,
    },
    "started_at": datetime.utcnow(),
}
```

- [ ] **Step 4: Add a shared ordinary-batch finalization helper**

```python
def _finalize_ordinary_batch_statistics(db, *, batch_id: str, status: str) -> None:
    batch = batch_tasks.get(batch_id)
    if not batch:
        return
    finalize_batch_statistics(
        db,
        batch_context={
            "batch_id": batch_id,
            "status": status,
            "mode": batch["mode"],
            "pipeline_key": batch["pipeline_key"],
            "email_service_type": batch["email_service_type"],
            "email_service_id": batch["email_service_id"],
            "target_count": batch["total"],
            "task_uuids": batch["task_uuids"],
            "started_at": batch["started_at"],
            "completed_at": datetime.utcnow(),
            "config_snapshot": batch["config_snapshot"],
            "success_count": batch["success"],
            "failed_count": batch["failed"],
        },
    )
```

- [ ] **Step 5: Call the helper from both ordinary batch terminal paths**

```python
with get_db() as db:
    _finalize_ordinary_batch_statistics(db, batch_id=batch_id, status="completed")
```

Apply this to:
- `run_batch_parallel(...)`
- `run_batch_pipeline(...)`

Do **not** wire this into:
- `run_unlimited_batch_registration(...)`
- Outlook batch endpoints

- [ ] **Step 6: Run the focused tests again**

Run: `uv run pytest tests/test_registration_batch_routes.py -k "finalizes_statistics" -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/web/routes/registration.py tests/test_registration_batch_routes.py
git commit -m "feat: persist ordinary batch statistics"
```

---

### Task 4: Add batch statistics list/detail/compare APIs

**Files:**
- Create: `src/web/routes/registration_batch_stats.py`
- Modify: `src/web/routes/__init__.py`
- Modify: `src/database/crud.py`
- Test: `tests/test_registration_batch_stats_routes.py`

- [ ] **Step 1: Write the failing route tests**

```python
def test_list_registration_batch_stats_returns_rows(client, seeded_batch_stats):
    response = client.get("/api/registration/batch-stats")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["items"][0]["batch_id"] == "batch-002"
```

```python
def test_get_registration_batch_stat_detail_returns_step_and_stage_stats(client, seeded_batch_stats):
    response = client.get(f"/api/registration/batch-stats/{seeded_batch_stats[0].id}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == seeded_batch_stats[0].id
    assert body["step_stats"][0]["step_key"] == "create_email"
    assert body["stage_stats"][0]["stage_key"] == "signup_prepare"
```

```python
def test_compare_registration_batch_stats_returns_summary_step_and_stage_diffs(client, seeded_batch_stats):
    response = client.post("/api/registration/batch-stats/compare", json={"left_id": 1, "right_id": 2})
    assert response.status_code == 200
    body = response.json()
    assert "summary_diff" in body
    assert "step_diffs" in body
    assert "stage_diffs" in body
```

- [ ] **Step 2: Run the route tests to verify failure**

Run: `uv run pytest tests/test_registration_batch_stats_routes.py -v`

Expected: FAIL because the router and helpers do not exist yet.

- [ ] **Step 3: Implement CRUD queries for list/detail lookup**

```python
def list_registration_batch_stats(db: Session, *, status: str | None = None, pipeline_key: str | None = None, skip: int = 0, limit: int = 50) -> list[RegistrationBatchStat]:
    query = db.query(RegistrationBatchStat).order_by(RegistrationBatchStat.started_at.desc(), RegistrationBatchStat.id.desc())
    if status is not None:
        query = query.filter(RegistrationBatchStat.status == status)
    if pipeline_key is not None:
        query = query.filter(RegistrationBatchStat.pipeline_key == pipeline_key)
    return query.offset(skip).limit(limit).all()
```

- [ ] **Step 4: Implement the router and register it**

```python
@router.get("")
async def list_batch_stats(...):
    ...

@router.get("/{stat_id}")
async def get_batch_stat_detail(stat_id: int):
    ...

@router.post("/compare")
async def compare_batch_stats(request: BatchStatsCompareRequest):
    ...
```

Register under:

```python
api_router.include_router(
    registration_batch_stats_router,
    prefix="/registration/batch-stats",
    tags=["registration-batch-stats"],
)
```

- [ ] **Step 5: Run the route tests to verify they pass**

Run: `uv run pytest tests/test_registration_batch_stats_routes.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/database/crud.py src/web/routes/__init__.py src/web/routes/registration_batch_stats.py tests/test_registration_batch_stats_routes.py
git commit -m "feat: add batch statistics APIs"
```

---

### Task 5: Build the standalone batch statistics page and A/B compare UI

**Files:**
- Create: `templates/registration_batch_stats.html`
- Create: `static/js/registration_batch_stats.js`
- Create: `tests/test_registration_batch_stats_page_assets.py`
- Create: `tests_runtime/registration_batch_stats_js_harness.py`
- Modify: `src/web/app.py`
- Modify: `static/css/style.css`

- [ ] **Step 1: Write the failing page asset tests**

```python
def test_registration_batch_stats_template_contains_list_detail_and_compare_containers():
    template = Path("templates/registration_batch_stats.html").read_text(encoding="utf-8")
    assert 'id="batch-stats-list"' in template
    assert 'id="batch-stats-detail"' in template
    assert 'id="batch-stats-compare"' in template
```

```python
def test_registration_batch_stats_page_requires_auth_and_renders_script():
    app = create_app()
    with TestClient(app) as client:
        unauthenticated = client.get("/registration-batch-stats", follow_redirects=False)
        assert unauthenticated.headers["location"] == "/login?next=/registration-batch-stats"
```

```python
def test_registration_batch_stats_script_loads_list_detail_and_compare_endpoints():
    script = Path("static/js/registration_batch_stats.js").read_text(encoding="utf-8")
    assert "/registration/batch-stats" in script
    assert "renderBatchStatsList" in script
    assert "renderBatchStatsCompare" in script
```

- [ ] **Step 2: Run the page asset tests to verify failure**

Run: `uv run pytest tests/test_registration_batch_stats_page_assets.py -v`

Expected: FAIL because the page, script, and route do not exist yet.

- [ ] **Step 3: Add the authenticated page route and template**

```python
@app.get("/registration-batch-stats", response_class=HTMLResponse)
async def registration_batch_stats_page(request: Request):
    if not _is_authenticated(request):
        return _redirect_to_login(request)
    return templates.TemplateResponse("registration_batch_stats.html", {"request": request})
```

- [ ] **Step 4: Implement the page client with 0/1/2 selection states**

```javascript
async function loadBatchStatsList() {
  const payload = await api.get("/registration/batch-stats");
  renderBatchStatsList(payload.items || []);
}

async function handleSelectionChange() {
  if (selectedIds.length === 1) {
    const detail = await api.get(`/registration/batch-stats/${selectedIds[0]}`);
    renderBatchStatsDetail(detail);
    return;
  }
  if (selectedIds.length === 2) {
    const compare = await api.post("/registration/batch-stats/compare", {
      left_id: selectedIds[0],
      right_id: selectedIds[1],
    });
    renderBatchStatsCompare(compare);
    return;
  }
}
```

- [ ] **Step 5: Add a JS harness test for list/detail/compare rendering and 2-item limit**

```python
def test_registration_batch_stats_script_renders_list_and_compare():
    result = run_registration_batch_stats_js_scenario("select_two_records_compare")
    assert result["apiGetPaths"] == ["/registration/batch-stats", "/registration/batch-stats/11"]
    assert result["apiPostCalls"][0]["path"] == "/registration/batch-stats/compare"
    assert "current_pipeline" in result["compareHtml"]
```

- [ ] **Step 6: Run the page asset tests to verify they pass**

Run: `uv run pytest tests/test_registration_batch_stats_page_assets.py -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add templates/registration_batch_stats.html static/js/registration_batch_stats.js static/css/style.css src/web/app.py tests/test_registration_batch_stats_page_assets.py tests_runtime/registration_batch_stats_js_harness.py
git commit -m "feat: add batch statistics dashboard"
```

---

### Task 6: Final verification and documentation touch-up

**Files:**
- Modify: `README.md` (only if feature entry or page link is needed)
- Test: `tests/test_database_session.py`
- Test: `tests/test_registration_batch_stats_models.py`
- Test: `tests/test_registration_batch_stats.py`
- Test: `tests/test_registration_batch_stats_routes.py`
- Test: `tests/test_registration_batch_routes.py`
- Test: `tests/test_registration_batch_stats_page_assets.py`
- Test: `tests/test_static_asset_versioning.py`

- [ ] **Step 1: Add a short README note only if the new page or feature summary needs it**

```markdown
- 支持普通批量注册历史统计记录、详情复盘与 A/B 对比报表
```

- [ ] **Step 2: Run the focused verification suite**

Run:

```bash
uv run pytest \
  tests/test_database_session.py \
  tests/test_registration_batch_stats_models.py \
  tests/test_registration_batch_stats.py \
  tests/test_registration_batch_stats_routes.py \
  tests/test_registration_batch_routes.py \
  tests/test_registration_batch_stats_page_assets.py \
  tests/test_static_asset_versioning.py -v
```

Expected: PASS.

- [ ] **Step 3: Run adjacent smoke coverage**

Run:

```bash
uv run pytest \
  tests/test_registration_page_assets.py \
  tests/test_registration_experiment_routes.py \
  tests/test_registration_experiment_page_assets.py -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add README.md tests test_runtime src templates static
git commit -m "feat: add ordinary batch statistics reporting"
```

---

## Notes for implementers

- Follow `@superpowers:test-driven-development` strictly on each task: write failing test first, verify failure, then implement the minimum change.
- Keep the new statistics feature isolated from `ExperimentBatch`; do not overload experiment models with ordinary batch semantics.
- Only ordinary `/registration/batch` flows are in scope. Do not wire this into unlimited or Outlook batch paths in this plan.
- Reuse existing patterns from:
  - `src/core/pipeline/experiments.py`
  - `src/web/routes/registration_experiments.py`
  - `tests/test_registration_experiment_routes.py`
  - `tests/test_registration_experiment_page_assets.py`

