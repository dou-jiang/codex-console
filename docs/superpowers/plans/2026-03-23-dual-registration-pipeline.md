# Dual Registration Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a shared step-based registration pipeline framework that runs both the current implementation and a codexgen-derived implementation inside the same API/UI/task system, with proxy preflight, pair experiments, step timing, and account survival tracking.

**Architecture:** Add a new `src/core/pipeline/` package that owns pipeline definitions, step execution, proxy preflight, and experiment orchestration. Keep the existing registration logic and the codexgen protocol logic as separate step implementations behind a common runner, while unifying email services, persistence, and reporting APIs/UI.

**Tech Stack:** FastAPI, SQLAlchemy ORM, existing scheduler/background runner patterns, curl_cffi HTTP sessions, Jinja2 templates, vanilla JS UI harness tests, pytest.

---

## File Structure Map

### New backend files
- Create: `src/core/pipeline/__init__.py` — package exports for pipeline framework.
- Create: `src/core/pipeline/context.py` — `PipelineContext` dataclass and typed runtime state.
- Create: `src/core/pipeline/definitions.py` — `PipelineDefinition`, `StepDefinition`, result/status enums.
- Create: `src/core/pipeline/runner.py` — common pipeline runner that records step runs.
- Create: `src/core/pipeline/registry.py` — pipeline registry and lookup helpers.
- Create: `src/core/pipeline/proxy_preflight.py` — threaded proxy preflight and random allocation helpers.
- Create: `src/core/pipeline/steps/__init__.py` — step export surface.
- Create: `src/core/pipeline/steps/common.py` — shared steps (`get_proxy_ip`, `create_email`, `persist_account`, `schedule_survival_checks`, etc.).
- Create: `src/core/pipeline/steps/current.py` — current pipeline step implementations using `RegistrationEngine` internals.
- Create: `src/core/pipeline/steps/codexgen.py` — codexgen step implementations adapted to current services.
- Create: `src/core/pipeline/experiments.py` — pair creation, experiment aggregation helpers.
- Create: `src/core/account_survival.py` — survival probe/classification helpers.
- Create: `src/core/account_survival_dispatcher.py` — background poller for due survival checks.
- Create: `src/web/routes/registration_experiments.py` — experiment APIs.
- Create: `src/web/routes/account_survival.py` — survival APIs.

### Modified backend files
- Modify: `src/database/models.py` — extend `RegistrationTask`; add pipeline/experiment/proxy/survival ORM models.
- Modify: `src/database/session.py` — add SQLite/PostgreSQL migrations for new columns/tables.
- Modify: `src/database/crud.py` — CRUD for step runs, experiments, proxy preflight, survival checks.
- Modify: `src/core/registration_job.py` — dispatch registration by `pipeline_key` and return step-aware results.
- Modify: `src/core/register.py` — expose/reshape current logic into step-friendly operations without deleting existing flow.
- Modify: `src/web/routes/registration.py` — accept `pipeline_key`, call new pipeline runner, add step payloads to task responses.
- Modify: `src/web/routes/__init__.py` — register experiment and survival routers.
- Modify: `src/web/task_manager.py` — store/broadcast current step state and experiment progress.
- Modify: `src/web/app.py` — mount new page routes and start/stop survival dispatcher.

### New frontend files
- Create: `templates/registration_experiments.html` — experiment comparison + survival dashboard page.
- Create: `static/js/registration_experiments.js` — experiment page client logic.

### Modified frontend files
- Modify: `templates/index.html` — pipeline selector, experiment mode controls, step timing panel.
- Modify: `static/js/app.js` — include `pipeline_key`, task detail step rendering, experiment launch support.
- Modify: `static/css/style.css` — styles for step waterfall and experiment/survival cards.

### New tests
- Create: `tests/test_pipeline_models.py`
- Create: `tests/test_pipeline_runner.py`
- Create: `tests/test_proxy_preflight.py`
- Create: `tests/test_current_pipeline.py`
- Create: `tests/test_codexgen_pipeline.py`
- Create: `tests/test_registration_experiment_routes.py`
- Create: `tests/test_account_survival.py`
- Create: `tests/test_account_survival_dispatcher.py`
- Create: `tests/test_account_survival_routes.py`
- Create: `tests/test_registration_experiment_page_assets.py`

### Modified tests
- Modify: `tests/test_database_session.py`
- Modify: `tests/test_registration_batch_routes.py`
- Modify: `tests/test_registration_engine.py`
- Modify: `tests/test_registration_page_assets.py`
- Modify: `tests_runtime/app_js_harness.py`

---

### Task 1: Add database support for pipelines, experiments, proxy preflight, and survival checks

**Files:**
- Modify: `src/database/models.py`
- Modify: `src/database/session.py`
- Modify: `src/database/crud.py`
- Test: `tests/test_database_session.py`
- Test: `tests/test_pipeline_models.py`

- [ ] **Step 1: Write failing migration tests for the new schema**

```python
# tests/test_pipeline_models.py
from src.database.models import RegistrationTask


def test_registration_task_supports_pipeline_metadata(temp_db):
    task = RegistrationTask(task_uuid="task-1", pipeline_key="current_pipeline", pair_key="pair-1")
    temp_db.add(task)
    temp_db.commit()
    assert task.pipeline_key == "current_pipeline"
    assert task.pair_key == "pair-1"
```

```python
# tests/test_database_session.py
assert any('ALTER TABLE "registration_tasks" ADD COLUMN IF NOT EXISTS "pipeline_key" VARCHAR(64)' in stmt for stmt in connection.statements)
assert any('ALTER TABLE "registration_tasks" ADD COLUMN IF NOT EXISTS "pair_key" VARCHAR(64)' in stmt for stmt in connection.statements)
```

- [ ] **Step 2: Run the focused schema tests and confirm they fail**

Run: `pytest tests/test_database_session.py tests/test_pipeline_models.py -v`
Expected: FAIL with missing columns / missing ORM models.

- [ ] **Step 3: Add ORM models and migration entries**

```python
class PipelineStepRun(Base):
    __tablename__ = "pipeline_step_runs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    task_uuid = Column(String(36), index=True, nullable=False)
    pipeline_key = Column(String(64), nullable=False, index=True)
    step_key = Column(String(64), nullable=False)
    step_order = Column(Integer, nullable=False)
    status = Column(String(20), nullable=False, default="pending")
    duration_ms = Column(Integer)
```

```python
POSTGRESQL_MIGRATIONS.extend([
    ("registration_tasks", "pipeline_key", "VARCHAR(64)"),
    ("registration_tasks", "pair_key", "VARCHAR(64)"),
    ("registration_tasks", "experiment_batch_id", "INTEGER"),
])
```

- [ ] **Step 4: Add CRUD helpers for the new models**

```python
def create_pipeline_step_run(db: Session, **kwargs) -> PipelineStepRun:
    row = PipelineStepRun(**kwargs)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row
```

- [ ] **Step 5: Run the focused schema tests again**

Run: `pytest tests/test_database_session.py tests/test_pipeline_models.py -v`
Expected: PASS.

- [ ] **Step 6: Commit the schema foundation**

```bash
git add src/database/models.py src/database/session.py src/database/crud.py tests/test_database_session.py tests/test_pipeline_models.py
git commit -m "feat: add pipeline and experiment persistence models"
```

### Task 2: Build the shared pipeline framework

**Files:**
- Create: `src/core/pipeline/__init__.py`
- Create: `src/core/pipeline/context.py`
- Create: `src/core/pipeline/definitions.py`
- Create: `src/core/pipeline/runner.py`
- Create: `src/core/pipeline/registry.py`
- Test: `tests/test_pipeline_runner.py`

- [ ] **Step 1: Write failing tests for pipeline context and runner ordering**

```python
from src.core.pipeline.context import PipelineContext
from src.core.pipeline.runner import PipelineRunner
from src.core.pipeline.definitions import PipelineDefinition, StepDefinition


def test_runner_records_step_durations_in_order(fake_db):
    calls = []

    def step_one(ctx):
        calls.append("step_one")
        return {"email": "a@example.com"}

    def step_two(ctx):
        calls.append(ctx.email)
        return {"done": True}

    pipeline = PipelineDefinition(
        pipeline_key="demo",
        steps=[
            StepDefinition("create_email", step_one),
            StepDefinition("finish", step_two),
        ],
    )
    ctx = PipelineContext(task_uuid="task-1", pipeline_key="demo")
    PipelineRunner(fake_db).run(pipeline, ctx)
    assert calls == ["step_one", "a@example.com"]
```

- [ ] **Step 2: Run the runner tests and confirm they fail**

Run: `pytest tests/test_pipeline_runner.py -v`
Expected: FAIL with missing modules/classes.

- [ ] **Step 3: Implement typed pipeline primitives**

```python
@dataclass
class PipelineContext:
    task_uuid: str
    pipeline_key: str
    experiment_batch_id: int | None = None
    pair_key: str | None = None
    proxy_url: str | None = None
    email: str | None = None
    password: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
```

```python
@dataclass(frozen=True)
class StepDefinition:
    step_key: str
    handler: Callable[[PipelineContext], dict[str, Any] | None]
    impl_key: str | None = None
```

- [ ] **Step 4: Implement the runner with DB-backed step records**

```python
class PipelineRunner:
    def run(self, pipeline: PipelineDefinition, ctx: PipelineContext) -> PipelineContext:
        for order, step in enumerate(pipeline.steps, start=1):
            started_at = datetime.utcnow()
            update_step_status(...)
            payload = step.handler(ctx) or {}
            for key, value in payload.items():
                setattr(ctx, key, value)
            finalize_step_run(...)
        return ctx
```

- [ ] **Step 5: Add registry helpers for pipeline lookup**

```python
PIPELINE_REGISTRY: dict[str, PipelineDefinition] = {}

def register_pipeline(definition: PipelineDefinition) -> None:
    PIPELINE_REGISTRY[definition.pipeline_key] = definition
```

- [ ] **Step 6: Run the runner tests again**

Run: `pytest tests/test_pipeline_runner.py -v`
Expected: PASS.

- [ ] **Step 7: Commit the pipeline core**

```bash
git add src/core/pipeline tests/test_pipeline_runner.py
git commit -m "feat: add shared registration pipeline framework"
```

### Task 3: Add threaded proxy preflight and random per-task proxy allocation

**Files:**
- Create: `src/core/pipeline/proxy_preflight.py`
- Modify: `src/database/crud.py`
- Test: `tests/test_proxy_preflight.py`

- [ ] **Step 1: Write failing tests for proxy preflight and allocation**

```python
from src.core.pipeline.proxy_preflight import run_proxy_preflight, choose_available_proxy


def test_choose_available_proxy_returns_random_available_result(monkeypatch):
    proxy_rows = [
        {"proxy_id": 1, "proxy_url": "http://a", "status": "available"},
        {"proxy_id": 2, "proxy_url": "http://b", "status": "available"},
    ]
    monkeypatch.setattr("random.choice", lambda items: items[-1])
    selected = choose_available_proxy(proxy_rows)
    assert selected["proxy_id"] == 2
```

- [ ] **Step 2: Run the proxy tests and confirm they fail**

Run: `pytest tests/test_proxy_preflight.py -v`
Expected: FAIL with missing module/functions.

- [ ] **Step 3: Implement threaded preflight**

```python
with ThreadPoolExecutor(max_workers=min(32, len(proxies) or 1)) as pool:
    futures = [pool.submit(check_single_proxy, proxy) for proxy in proxies]
```

- [ ] **Step 4: Persist preflight results and expose random selection**

```python
def choose_available_proxy(results: list[dict[str, Any]]) -> dict[str, Any]:
    available = [item for item in results if item["status"] == "available"]
    if not available:
        raise RuntimeError("no available proxy")
    return random.choice(available)
```

- [ ] **Step 5: Run the proxy tests again**

Run: `pytest tests/test_proxy_preflight.py -v`
Expected: PASS.

- [ ] **Step 6: Commit proxy preflight support**

```bash
git add src/core/pipeline/proxy_preflight.py src/database/crud.py tests/test_proxy_preflight.py
git commit -m "feat: add proxy preflight and allocation helpers"
```

### Task 4: Adapt the current registration logic into step implementations

**Files:**
- Create: `src/core/pipeline/steps/common.py`
- Create: `src/core/pipeline/steps/current.py`
- Modify: `src/core/register.py`
- Test: `tests/test_current_pipeline.py`
- Test: `tests/test_registration_engine.py`

- [ ] **Step 1: Write failing tests for the current pipeline step sequence**

```python
from src.core.pipeline.registry import get_pipeline


def test_current_pipeline_runs_signup_then_relogin_steps(fake_current_pipeline_env):
    pipeline = get_pipeline("current_pipeline")
    ctx = fake_current_pipeline_env.context
    fake_current_pipeline_env.runner.run(pipeline, ctx)
    assert ctx.email == "tester@example.com"
    assert ctx.metadata["token_acquired_via_relogin"] is True
```

- [ ] **Step 2: Run the current pipeline tests and confirm they fail**

Run: `pytest tests/test_current_pipeline.py tests/test_registration_engine.py -v`
Expected: FAIL because `current_pipeline` is not registered and the engine does not expose step-friendly adapters.

- [ ] **Step 3: Expose step-safe adapters from `RegistrationEngine`**

```python
class RegistrationEngine:
    def run_create_email_step(self) -> dict[str, Any]:
        if not self._create_email():
            raise RuntimeError("create_email failed")
        return {"email": self.email, "email_info": self.email_info}
```

- [ ] **Step 4: Implement shared/common steps**

```python
def create_email_step(ctx: PipelineContext) -> dict[str, Any]:
    info = ctx.email_service.create_email()
    return {"email": info["email"], "email_info": info}
```

- [ ] **Step 5: Register `current_pipeline` using the new step bindings**

```python
register_pipeline(
    PipelineDefinition(
        pipeline_key="current_pipeline",
        steps=[
            StepDefinition("get_proxy_ip", get_proxy_ip_step, impl_key="common.get_proxy_ip"),
            StepDefinition("create_email", current_create_email_step, impl_key="current.create_email"),
            StepDefinition("prepare_authorize_flow", current_prepare_authorize_flow_step, impl_key="current.prepare_authorize_flow"),
            # ...
        ],
    )
)
```

- [ ] **Step 6: Run the current pipeline tests again**

Run: `pytest tests/test_current_pipeline.py tests/test_registration_engine.py -v`
Expected: PASS.

- [ ] **Step 7: Commit the current pipeline adapter**

```bash
git add src/core/pipeline/steps/common.py src/core/pipeline/steps/current.py src/core/register.py tests/test_current_pipeline.py tests/test_registration_engine.py
git commit -m "feat: adapt current registration flow to step pipeline"
```

### Task 5: Adapt codexgen logic into a second pipeline and switch it to shared email services

**Files:**
- Create: `src/core/pipeline/steps/codexgen.py`
- Modify: `src/core/registration_job.py`
- Modify: `src/services/__init__.py` (only if imports/helpers are needed)
- Test: `tests/test_codexgen_pipeline.py`

- [ ] **Step 1: Write failing tests for the codexgen pipeline using shared email services**

```python
from src.core.pipeline.registry import get_pipeline


def test_codexgen_pipeline_reads_email_from_shared_service(fake_codexgen_env):
    pipeline = get_pipeline("codexgen_pipeline")
    ctx = fake_codexgen_env.context
    fake_codexgen_env.runner.run(pipeline, ctx)
    assert fake_codexgen_env.email_service.create_calls == 1
    assert ctx.email.endswith("@example.com")
```

- [ ] **Step 2: Run the codexgen pipeline tests and confirm they fail**

Run: `pytest tests/test_codexgen_pipeline.py -v`
Expected: FAIL because `codexgen_pipeline` is missing and still depends on local email helpers.

- [ ] **Step 3: Move the codexgen protocol logic behind step handlers**

```python
def codexgen_submit_signup_email(ctx: PipelineContext) -> dict[str, Any]:
    response = protocol_runtime.step0_init_oauth_session(ctx.email)
    if not response.success:
        raise RuntimeError(response.error_message)
    return {"device_id": response.device_id}
```

- [ ] **Step 4: Replace codexgen email creation and OTP polling with shared services**

```python
def codexgen_wait_signup_otp(ctx: PipelineContext) -> dict[str, Any]:
    code = ctx.email_service.get_verification_code(
        email=ctx.email,
        email_id=ctx.email_info.get("service_id"),
        timeout=120,
        pattern=OTP_CODE_PATTERN,
        otp_sent_at=ctx.otp_sent_at,
    )
    return {"otp_code": code}
```

- [ ] **Step 5: Register `codexgen_pipeline` and teach `run_registration_job` to dispatch by `pipeline_key`**

```python
if pipeline_key == "codexgen_pipeline":
    pipeline = get_pipeline("codexgen_pipeline")
else:
    pipeline = get_pipeline("current_pipeline")
```

- [ ] **Step 6: Run the codexgen pipeline tests again**

Run: `pytest tests/test_codexgen_pipeline.py -v`
Expected: PASS.

- [ ] **Step 7: Commit the codexgen pipeline integration**

```bash
git add src/core/pipeline/steps/codexgen.py src/core/registration_job.py tests/test_codexgen_pipeline.py
git commit -m "feat: add codexgen registration pipeline"
```

### Task 6: Integrate `pipeline_key` and step tracking into the registration APIs and task manager

**Files:**
- Modify: `src/web/routes/registration.py`
- Modify: `src/web/task_manager.py`
- Modify: `src/database/crud.py`
- Test: `tests/test_registration_batch_routes.py`

- [ ] **Step 1: Write failing API tests for pipeline selection and step-aware task responses**

```python
def test_start_registration_persists_pipeline_key(route_db, monkeypatch):
    response = registration_routes.RegistrationTaskCreate(email_service_type="tempmail", pipeline_key="codexgen_pipeline")
    # call route and assert stored task has pipeline_key set
```

- [ ] **Step 2: Run the registration route tests and confirm they fail**

Run: `pytest tests/test_registration_batch_routes.py -v`
Expected: FAIL because request models and CRUD paths do not include `pipeline_key` / step metadata.

- [ ] **Step 3: Extend request/response models and task manager step state**

```python
class RegistrationTaskCreate(BaseModel):
    email_service_type: str = "tempmail"
    pipeline_key: str = "current_pipeline"
```

```python
_task_steps: dict[str, list[dict[str, Any]]] = {}
```

- [ ] **Step 4: Route execution through the pipeline runner**

```python
job_result = run_registration_job(
    db=db,
    email_service_type=email_service_type,
    pipeline_key=pipeline_key,
    proxy=actual_proxy_url,
    ...
)
```

- [ ] **Step 5: Run the registration route tests again**

Run: `pytest tests/test_registration_batch_routes.py -v`
Expected: PASS.

- [ ] **Step 6: Commit the API/task-manager integration**

```bash
git add src/web/routes/registration.py src/web/task_manager.py src/database/crud.py tests/test_registration_batch_routes.py
git commit -m "feat: wire pipelines into registration task APIs"
```

### Task 7: Add experiment batch orchestration and comparison APIs

**Files:**
- Create: `src/core/pipeline/experiments.py`
- Create: `src/web/routes/registration_experiments.py`
- Modify: `src/web/routes/__init__.py`
- Modify: `src/web/task_manager.py`
- Test: `tests/test_registration_experiment_routes.py`

- [ ] **Step 1: Write failing tests for experiment creation and pair generation**

```python
def test_create_experiment_batch_creates_paired_tasks(route_db, monkeypatch):
    response = client.post("/api/registration/experiments", json={"count": 2, ...})
    body = response.json()
    assert body["total_tasks"] == 4
    assert {task["pipeline_key"] for task in body["tasks"]} == {"current_pipeline", "codexgen_pipeline"}
```

- [ ] **Step 2: Run the experiment route tests and confirm they fail**

Run: `pytest tests/test_registration_experiment_routes.py -v`
Expected: FAIL because the experiment router does not exist.

- [ ] **Step 3: Implement experiment creation and pair helpers**

```python
def build_pairs(count: int) -> list[tuple[str, str]]:
    return [(f"pair-{i:04d}", pipeline_key) for i in range(1, count + 1) for pipeline_key in ("current_pipeline", "codexgen_pipeline")]
```

- [ ] **Step 4: Add overview, pairs, and step-summary endpoints**

```python
@router.get("/{experiment_id}/steps")
def get_experiment_step_summary(...):
    return build_step_summary(...)
```

- [ ] **Step 5: Run the experiment route tests again**

Run: `pytest tests/test_registration_experiment_routes.py -v`
Expected: PASS.

- [ ] **Step 6: Commit the experiment backend**

```bash
git add src/core/pipeline/experiments.py src/web/routes/registration_experiments.py src/web/routes/__init__.py src/web/task_manager.py tests/test_registration_experiment_routes.py
git commit -m "feat: add paired registration experiments"
```

### Task 8: Add account survival scheduling, probing, and APIs

**Files:**
- Create: `src/core/account_survival.py`
- Create: `src/core/account_survival_dispatcher.py`
- Create: `src/web/routes/account_survival.py`
- Modify: `src/web/routes/__init__.py`
- Modify: `src/web/app.py`
- Test: `tests/test_account_survival.py`
- Test: `tests/test_account_survival_dispatcher.py`
- Test: `tests/test_account_survival_routes.py`

- [ ] **Step 1: Write failing tests for survival classification and scheduling**

```python
from src.core.account_survival import classify_survival_result


def test_classify_survival_result_marks_invalid_refresh_as_dead():
    assert classify_survival_result(signal_type="refresh_invalid", status_code=401) == "dead"
```

```python
def test_dispatcher_claims_due_checks_and_records_result(fake_survival_repo):
    dispatcher = AccountSurvivalDispatcher(fake_survival_repo)
    dispatcher.dispatch_due_checks_once()
    assert fake_survival_repo.completed == 1
```

- [ ] **Step 2: Run the survival tests and confirm they fail**

Run: `pytest tests/test_account_survival.py tests/test_account_survival_dispatcher.py tests/test_account_survival_routes.py -v`
Expected: FAIL because the survival modules do not exist.

- [ ] **Step 3: Implement survival probe and classification helpers**

```python
def classify_survival_result(*, signal_type: str, status_code: int | None) -> str:
    if signal_type in {"refresh_ok", "auth_ok"}:
        return "healthy"
    if status_code in {401, 403}:
        return "dead"
    return "warning"
```

- [ ] **Step 4: Implement the due-check dispatcher and app lifecycle hooks**

```python
@app.on_event("startup")
async def startup_event():
    app.state.account_survival_dispatcher = AccountSurvivalDispatcher()
    app.state.account_survival_dispatcher.start()
```

- [ ] **Step 5: Add manual trigger and summary APIs**

```python
@router.post("/survival-checks/run")
def run_survival_checks(...):
    return {"scheduled": scheduled_count}
```

- [ ] **Step 6: Run the survival tests again**

Run: `pytest tests/test_account_survival.py tests/test_account_survival_dispatcher.py tests/test_account_survival_routes.py -v`
Expected: PASS.

- [ ] **Step 7: Commit the survival system**

```bash
git add src/core/account_survival.py src/core/account_survival_dispatcher.py src/web/routes/account_survival.py src/web/routes/__init__.py src/web/app.py tests/test_account_survival.py tests/test_account_survival_dispatcher.py tests/test_account_survival_routes.py
git commit -m "feat: add account survival tracking"
```

### Task 9: Extend the registration page for pipeline selection and step timing

**Files:**
- Modify: `templates/index.html`
- Modify: `static/js/app.js`
- Modify: `static/css/style.css`
- Modify: `tests_runtime/app_js_harness.py`
- Modify: `tests/test_registration_page_assets.py`

- [ ] **Step 1: Write failing UI tests for pipeline selection and step timing placeholders**

```python
def test_registration_template_contains_pipeline_selector_and_step_panel():
    template = Path("templates/index.html").read_text(encoding="utf-8")
    assert 'id="pipeline-key"' in template
    assert 'id="task-step-waterfall"' in template
```

```python
def test_app_js_includes_pipeline_key_in_registration_payload():
    result = run_app_js_scenario("pipeline_selection_request")
    assert result["request_payload"]["pipeline_key"] == "codexgen_pipeline"
```

- [ ] **Step 2: Run the registration page asset tests and confirm they fail**

Run: `pytest tests/test_registration_page_assets.py -v`
Expected: FAIL because the selector and request payload are missing.

- [ ] **Step 3: Add the pipeline selector and step waterfall containers**

```html
<select id="pipeline-key" name="pipeline_key">
  <option value="current_pipeline">当前流水线</option>
  <option value="codexgen_pipeline">codexgen 流水线</option>
</select>
<div id="task-step-waterfall" class="step-waterfall"></div>
```

- [ ] **Step 4: Update `app.js` to post `pipeline_key` and render step timing**

```javascript
requestData.pipeline_key = elements.pipelineKey.value || 'current_pipeline';
```

```javascript
function renderTaskSteps(steps) {
  elements.taskStepWaterfall.innerHTML = steps.map(step => `<div>${step.step_key}: ${step.duration_ms}ms</div>`).join('');
}
```

- [ ] **Step 5: Run the registration page asset tests again**

Run: `pytest tests/test_registration_page_assets.py -v`
Expected: PASS.

- [ ] **Step 6: Commit the registration UI changes**

```bash
git add templates/index.html static/js/app.js static/css/style.css tests_runtime/app_js_harness.py tests/test_registration_page_assets.py
git commit -m "feat: add pipeline selector and step timing UI"
```

### Task 10: Build the experiment comparison + survival dashboard page

**Files:**
- Create: `templates/registration_experiments.html`
- Create: `static/js/registration_experiments.js`
- Modify: `static/css/style.css`
- Modify: `src/web/app.py`
- Test: `tests/test_registration_experiment_page_assets.py`

- [ ] **Step 1: Write failing asset tests for the experiment page**

```python
def test_experiment_template_contains_summary_step_and_survival_sections():
    template = Path("templates/registration_experiments.html").read_text(encoding="utf-8")
    assert 'id="experiment-summary"' in template
    assert 'id="experiment-step-compare"' in template
    assert 'id="survival-summary"' in template
```

- [ ] **Step 2: Run the experiment page tests and confirm they fail**

Run: `pytest tests/test_registration_experiment_page_assets.py -v`
Expected: FAIL because the page and script do not exist.

- [ ] **Step 3: Create the experiment page and wire it into the app**

```python
@app.get("/registration-experiments", response_class=HTMLResponse)
async def registration_experiments_page(request: Request):
    if not _is_authenticated(request):
        return _redirect_to_login(request)
    return templates.TemplateResponse("registration_experiments.html", {"request": request})
```

- [ ] **Step 4: Implement the dashboard client script**

```javascript
async function loadExperimentSummary(experimentId) {
  const [summary, steps, survival] = await Promise.all([
    api.get(`/registration/experiments/${experimentId}`),
    api.get(`/registration/experiments/${experimentId}/steps`),
    api.get(`/accounts/survival-summary?experiment_batch_id=${experimentId}`),
  ]);
  renderSummary(summary);
  renderStepCompare(steps);
  renderSurvival(survival);
}
```

- [ ] **Step 5: Run the experiment page tests again**

Run: `pytest tests/test_registration_experiment_page_assets.py -v`
Expected: PASS.

- [ ] **Step 6: Commit the experiment dashboard**

```bash
git add templates/registration_experiments.html static/js/registration_experiments.js static/css/style.css src/web/app.py tests/test_registration_experiment_page_assets.py
git commit -m "feat: add experiment comparison dashboard"
```

### Task 11: Final verification and documentation touch-up

**Files:**
- Modify: `README.md`
- Modify: `docs/2026-03-23-registration-optimization-proposal.md` (only if links or references are needed)
- Test: `tests/test_registration_batch_routes.py`
- Test: `tests/test_registration_page_assets.py`
- Test: `tests/test_registration_experiment_routes.py`
- Test: `tests/test_registration_experiment_page_assets.py`
- Test: `tests/test_account_survival_routes.py`

- [ ] **Step 1: Write/adjust docs assertions only if new page links or README references are added**

```markdown
- Added pipeline selector for current and codexgen flows.
- Added /registration-experiments comparison dashboard.
- Added automatic survival checks at 1h / 24h / 72h / 7d.
```

- [ ] **Step 2: Run the focused integration suite**

Run:
```bash
pytest \
  tests/test_database_session.py \
  tests/test_pipeline_models.py \
  tests/test_pipeline_runner.py \
  tests/test_proxy_preflight.py \
  tests/test_current_pipeline.py \
  tests/test_codexgen_pipeline.py \
  tests/test_registration_batch_routes.py \
  tests/test_registration_experiment_routes.py \
  tests/test_account_survival.py \
  tests/test_account_survival_dispatcher.py \
  tests/test_account_survival_routes.py \
  tests/test_registration_page_assets.py \
  tests/test_registration_experiment_page_assets.py -v
```
Expected: PASS.

- [ ] **Step 3: Run a smoke test for the app boot path**

Run: `pytest tests/test_static_asset_versioning.py tests/test_scheduler_service.py -v`
Expected: PASS.

- [ ] **Step 4: Update README / docs if needed and rerun doc-adjacent tests**

Run: `pytest tests/test_registration_page_assets.py tests/test_registration_experiment_page_assets.py -v`
Expected: PASS.

- [ ] **Step 5: Commit the final integrated feature**

```bash
git add README.md docs/2026-03-23-registration-optimization-proposal.md
git add src tests templates static

git commit -m "feat: add dual registration pipeline comparison system"
```

## Implementation Notes

- Use `@superpowers:test-driven-development` before writing any production code in each task.
- Use `@superpowers:verification-before-completion` before claiming any task is done.
- Preserve existing endpoints and response fields unless the task explicitly adds new ones.
- Prefer wrapping current/codexgen logic instead of rewriting it in place.
- Keep `codexgen_pipeline` email acquisition and OTP polling on the shared service layer; do not reintroduce local mailbox APIs.
- Treat proxy preflight as batch/experiment preparation work, not as a timed registration business step.
- Keep the current flow's IP location validation logic; model it as a current-pipeline-specific step or helper after proxy selection instead of folding it into proxy preflight.
