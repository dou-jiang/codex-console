# Registration Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** improve registration success rate, reduce runtime pressure during high-frequency execution, and move execution responsibility onto the migrated task path without breaking current product behavior.

**Architecture:** keep the current public entrypoints, but move internal behavior toward shared OTP policy helpers, buffered task logging, decomposed registration orchestration, and worker-first execution. The old Web registration routes remain compatible, but they should delegate more work to the migrated task/service boundaries instead of owning execution directly.

**Tech Stack:** Python 3.10+, FastAPI, SQLAlchemy, pytest, curl-cffi, migrated `apps/api` + `apps/worker` + `packages/*` boundaries

---

## File Structure

### Shared OTP policy

- Create: `src/services/otp_policy.py`
- Modify: `src/services/temp_mail.py`
- Modify: `src/services/freemail.py`
- Modify: `src/services/duck_mail.py`
- Modify: `src/services/outlook/service.py`
- Test: `tests/test_temp_mail_service.py`
- Test: `tests/test_duck_mail_service.py`
- Create: `tests/test_freemail_service.py`

Responsibility:

- `src/services/otp_policy.py` holds provider-agnostic OTP candidate normalization and ranking
- each provider keeps its own fetch logic, but uses the same OTP decision rules

### Buffered task logging

- Modify: `packages/account_store/logs.py`
- Modify: `apps/worker/main.py`
- Test: `tests/unit/account_store/test_task_log_store.py`
- Test: `tests/unit/apps/test_worker_runner.py`

Responsibility:

- `TaskLogStore` gains small-batch append support
- worker logging moves from immediate per-line writes toward buffered flushes

### Registration-core decomposition

- Create: `packages/registration_core/runtime.py`
- Create: `packages/registration_core/token_flow.py`
- Create: `packages/registration_core/result_builder.py`
- Modify: `packages/registration_core/engine.py`
- Modify: `src/core/register.py`
- Test: `tests/unit/registration_core/test_engine.py`
- Modify: `tests/test_registration_engine.py`

Responsibility:

- keep the current engine contract stable
- split orchestration helpers into smaller units without changing current user-visible behavior

### Task-path promotion

- Modify: `apps/api/task_service.py`
- Modify: `apps/worker/main.py`
- Modify: `src/web/routes/registration.py`
- Modify: `src/web/task_manager.py`
- Test: `tests/unit/apps/test_register_routes.py`
- Test: `tests/unit/legacy/test_registration_route_bridge.py`
- Test: `tests/unit/legacy/test_registration_batch_bridge.py`
- Test: `tests/unit/legacy/test_registration_batch_execution_bridge.py`

Responsibility:

- web entrypoints become task launchers
- worker/service path becomes the default executor

### Heavy-flow isolation

- Modify: `src/web/routes/payment.py`
- Modify: `apps/api/payment_task_service.py`
- Modify: `src/core/register.py`
- Test: `tests/unit/legacy/test_payment_bind_bridge.py`

Responsibility:

- keep registration result clean
- make heavy payment/browser follow-up explicit and separable

## Task 1: Shared OTP Policy Helper

**Files:**
- Create: `src/services/otp_policy.py`
- Modify: `src/services/temp_mail.py`
- Test: `tests/test_temp_mail_service.py`

- [ ] **Step 1: Write the failing shared-OTP-policy tests**

Add tests covering:

```python
def test_rank_prefers_recent_semantic_match():
    ...

def test_rank_skips_last_used_mail():
    ...
```

- [ ] **Step 2: Run the focused tests to verify the new helper does not exist yet**

Run:

```bash
pytest tests/test_temp_mail_service.py -q
```

Expected:

- at least one new test fails because shared helper behavior is missing

- [ ] **Step 3: Write the minimal helper**

Create `src/services/otp_policy.py` with small focused functions for:

- candidate normalization
- staleness filtering
- semantic-vs-generic ranking
- last-used-mail skipping

- [ ] **Step 4: Switch `TempMail` to the helper without changing its fetch strategy**

Keep:

- current API fallback order
- current detail fetch behavior

Move only candidate selection and ranking to the new helper.

- [ ] **Step 5: Run the focused tests to verify `TempMail` still passes**

Run:

```bash
pytest tests/test_temp_mail_service.py -q
```

Expected:

- PASS

- [ ] **Step 6: Commit**

```bash
git add src/services/otp_policy.py tests/test_temp_mail_service.py src/services/temp_mail.py
git commit -m "refactor: extract shared otp selection policy"
```

## Task 2: Freemail and DuckMail OTP Alignment

**Files:**
- Modify: `src/services/freemail.py`
- Modify: `src/services/duck_mail.py`
- Create: `tests/test_freemail_service.py`
- Modify: `tests/test_duck_mail_service.py`

- [ ] **Step 1: Write the failing provider regression tests**

Add tests covering:

- Freemail ignores old mail when `otp_sent_at` is newer
- Freemail avoids reusing an already-consumed message
- DuckMail prefers the newest semantic OTP candidate

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_freemail_service.py tests/test_duck_mail_service.py -q
```

Expected:

- FAIL because current provider logic is still inconsistent

- [ ] **Step 3: Implement minimal provider alignment**

Use the helper from Task 1.

For `Freemail`:

- start honoring `otp_sent_at`
- preserve current API behavior
- add per-email last-used message tracking

For `DuckMail`:

- normalize candidates before extracting final code

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
pytest tests/test_freemail_service.py tests/test_duck_mail_service.py -q
```

Expected:

- PASS

- [ ] **Step 5: Commit**

```bash
git add src/services/freemail.py src/services/duck_mail.py tests/test_freemail_service.py tests/test_duck_mail_service.py
git commit -m "fix: align provider otp filtering rules"
```

## Task 3: Buffered Task Logging

**Files:**
- Modify: `packages/account_store/logs.py`
- Modify: `apps/worker/main.py`
- Modify: `tests/unit/account_store/test_task_log_store.py`
- Modify: `tests/unit/apps/test_worker_runner.py`

- [ ] **Step 1: Write the failing logging tests**

Add tests covering:

- `TaskLogStore.append_many()` preserves order
- worker flushes buffered logs on success
- worker flushes buffered logs on failure

- [ ] **Step 2: Run the focused tests to verify failure**

Run:

```bash
pytest tests/unit/account_store/test_task_log_store.py tests/unit/apps/test_worker_runner.py -q
```

Expected:

- FAIL because batch append / flush behavior does not exist yet

- [ ] **Step 3: Add minimal batch-log support**

Implement:

- `append_many()` in `TaskLogStore`
- small in-memory buffer in `WorkerRunner`
- explicit final flush in both success and exception paths

- [ ] **Step 4: Run the focused tests again**

Run:

```bash
pytest tests/unit/account_store/test_task_log_store.py tests/unit/apps/test_worker_runner.py -q
```

Expected:

- PASS

- [ ] **Step 5: Commit**

```bash
git add packages/account_store/logs.py apps/worker/main.py tests/unit/account_store/test_task_log_store.py tests/unit/apps/test_worker_runner.py
git commit -m "refactor: buffer worker task log writes"
```

## Task 4: Task Path Promotion in Web Registration Routes

**Files:**
- Modify: `apps/api/task_service.py`
- Modify: `src/web/routes/registration.py`
- Modify: `tests/unit/apps/test_register_routes.py`
- Modify: `tests/unit/legacy/test_registration_route_bridge.py`
- Modify: `tests/unit/legacy/test_registration_batch_bridge.py`
- Modify: `tests/unit/legacy/test_registration_batch_execution_bridge.py`

- [ ] **Step 1: Write failing tests for web-to-task delegation**

Add tests covering:

- single registration route creates task via migrated task service
- batch registration route creates task records via migrated task service
- legacy compatibility response shape remains stable

- [ ] **Step 2: Run the route-focused tests to verify failure**

Run:

```bash
pytest tests/unit/apps/test_register_routes.py tests/unit/legacy/test_registration_route_bridge.py tests/unit/legacy/test_registration_batch_bridge.py tests/unit/legacy/test_registration_batch_execution_bridge.py -q
```

Expected:

- FAIL where route code still owns more orchestration than intended

- [ ] **Step 3: Implement minimal delegation cleanup**

Reduce duplicate route-side logic by:

- centralizing task creation in `apps/api/task_service.py`
- using migrated store access consistently
- keeping current HTTP response shape unchanged

- [ ] **Step 4: Run the route-focused tests again**

Run:

```bash
pytest tests/unit/apps/test_register_routes.py tests/unit/legacy/test_registration_route_bridge.py tests/unit/legacy/test_registration_batch_bridge.py tests/unit/legacy/test_registration_batch_execution_bridge.py -q
```

Expected:

- PASS

- [ ] **Step 5: Commit**

```bash
git add apps/api/task_service.py src/web/routes/registration.py tests/unit/apps/test_register_routes.py tests/unit/legacy/test_registration_route_bridge.py tests/unit/legacy/test_registration_batch_bridge.py tests/unit/legacy/test_registration_batch_execution_bridge.py
git commit -m "refactor: route web registration through migrated task path"
```

## Task 5: Registration Core Decomposition Without Contract Breakage

**Files:**
- Create: `packages/registration_core/runtime.py`
- Create: `packages/registration_core/token_flow.py`
- Create: `packages/registration_core/result_builder.py`
- Modify: `packages/registration_core/engine.py`
- Modify: `src/core/register.py`
- Modify: `tests/unit/registration_core/test_engine.py`
- Modify: `tests/test_registration_engine.py`

- [ ] **Step 1: Write the failing decomposition tests**

Add tests that assert:

- orchestration helper boundaries are called in order
- the bridge still returns the same high-level result contract
- token/result assembly is still compatible

- [ ] **Step 2: Run the focused engine tests to verify failure**

Run:

```bash
pytest tests/unit/registration_core/test_engine.py tests/test_registration_engine.py -q
```

Expected:

- FAIL because the helper boundaries do not exist yet

- [ ] **Step 3: Extract the smallest useful helpers**

Move step orchestration into small files without changing the external contract.

Do not rewrite the whole registration flow in one pass.

- [ ] **Step 4: Run the focused engine tests again**

Run:

```bash
pytest tests/unit/registration_core/test_engine.py tests/test_registration_engine.py -q
```

Expected:

- PASS

- [ ] **Step 5: Commit**

```bash
git add packages/registration_core/runtime.py packages/registration_core/token_flow.py packages/registration_core/result_builder.py packages/registration_core/engine.py src/core/register.py tests/unit/registration_core/test_engine.py tests/test_registration_engine.py
git commit -m "refactor: decompose registration orchestration helpers"
```

## Task 6: Heavy-Flow Isolation

**Files:**
- Modify: `src/web/routes/payment.py`
- Modify: `apps/api/payment_task_service.py`
- Modify: `src/core/register.py`
- Modify: `tests/unit/legacy/test_payment_bind_bridge.py`

- [ ] **Step 1: Write the failing heavy-flow isolation tests**

Add tests asserting:

- registration success path returns cleanly without implicitly owning payment follow-up
- payment/bind bridges still work through explicit handoff

- [ ] **Step 2: Run the focused payment bridge tests**

Run:

```bash
pytest tests/unit/legacy/test_payment_bind_bridge.py -q
```

Expected:

- FAIL because handoff is still too entangled

- [ ] **Step 3: Implement explicit handoff boundaries**

Keep:

- compatibility for current entrypoints

Change:

- registration path should produce a result
- payment/bind follow-up should be invoked by explicit bridge/service code

- [ ] **Step 4: Run the focused payment bridge tests again**

Run:

```bash
pytest tests/unit/legacy/test_payment_bind_bridge.py -q
```

Expected:

- PASS

- [ ] **Step 5: Commit**

```bash
git add src/web/routes/payment.py apps/api/payment_task_service.py src/core/register.py tests/unit/legacy/test_payment_bind_bridge.py
git commit -m "refactor: isolate heavy payment follow-up from registration"
```

## Task 7: Full Regression Verification

**Files:**
- Modify: `README.md` if behavior expectations changed
- Modify: `docs/migration/2026-03-26-phase2-quickstart.md` if execution behavior changed

- [ ] **Step 1: Run the full migration regression suite**

Run:

```bash
pytest tests/unit/apps/test_entrypoints.py tests/unit/apps/test_main_entrypoints.py tests/unit/apps/test_script_entrypoints.py tests/unit/apps/test_register_routes.py tests/unit/apps/test_worker_runner.py tests/unit/legacy/test_registration_route_bridge.py tests/unit/legacy/test_registration_task_read_bridge.py tests/unit/legacy/test_registration_batch_bridge.py tests/unit/legacy/test_registration_batch_execution_bridge.py tests/unit/legacy/test_payment_bind_bridge.py tests/unit/registration_core/test_engine.py tests/test_registration_engine.py tests/test_temp_mail_service.py tests/test_duck_mail_service.py tests/test_freemail_service.py -q
```

Expected:

- PASS

- [ ] **Step 2: Update docs if runtime behavior or recommended startup flow changed**

Keep doc updates factual and small.

- [ ] **Step 3: Run final targeted verification after doc updates**

Run:

```bash
pytest tests/unit/apps/test_register_routes.py tests/unit/apps/test_worker_runner.py tests/unit/legacy/test_payment_bind_bridge.py tests/test_temp_mail_service.py tests/test_duck_mail_service.py tests/test_freemail_service.py -q
```

Expected:

- PASS

- [ ] **Step 4: Commit**

```bash
git add README.md docs/migration/2026-03-26-phase2-quickstart.md tests/test_freemail_service.py
git commit -m "docs: sync registration refactor behavior"
```

## Execution Notes

- Follow @test-driven-development strictly for each task
- Keep commits small and reversible
- Prefer behavior-preserving extraction over large rewrites
- Do not mix provider OTP changes with payment-flow isolation in one commit
- Re-run regression bridges after every task that touches `src/web/routes/registration.py` or `src/core/register.py`

## Recommended Execution Mode

Because this session already contains the design context and the user asked for completion, execute this plan inline in the current session, one task at a time, with verification after each slice.
