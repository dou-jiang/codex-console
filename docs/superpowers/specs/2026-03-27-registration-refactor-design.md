# Registration Refactor Design

**Date:** 2026-03-27

**Repository:** `codex-console`

**Goal:** make registration flows easier to maintain, improve registration success rate across email providers, and reduce server pressure during high-frequency execution without breaking the current usable product surface.

## Context

The current branch already introduced a new task-driven path with:

- `apps/api`
- `apps/worker`
- `packages/registration_core`
- `packages/email_providers`
- `packages/account_store`

However, the real registration execution still ultimately flows through the legacy core:

- `packages/registration_core/engine.py` bridges to `src/core/register.py`

That means the repository currently has a cleaner outer shell, but the most failure-prone and resource-heavy logic is still concentrated in the old core. At the same time, provider behavior is inconsistent:

- `TempMail` already contains stronger OTP filtering and de-duplication
- `Freemail` still uses simpler polling and code extraction
- Outlook handling is stronger in some paths but not unified with the new provider-side rules

This refactor is meant to turn the current migration line into a real operating baseline rather than a partial bridge.

## Non-Goals

These are explicitly out of scope for this wave:

- rewriting the full OpenAI registration flow from scratch
- changing the public product positioning or removing the old Web UI
- adding every new upstream email source immediately
- redesigning the frontend
- introducing an external queue broker in this pass

## User-Facing Outcomes

After this work:

1. registration runs should more consistently avoid old or irrelevant OTPs across providers
2. high-frequency execution should create less database and process pressure
3. the new API/worker path should be the preferred execution path
4. the old Web UI should remain usable, but act more like a task launcher than a direct executor
5. payment and browser-heavy follow-up flows should stop dragging the core registration path around

## Design Overview

The recommended approach is a staged internal replacement, not a big-bang rewrite.

### Stage 1: Provider OTP Policy Unification

Extract the strongest OTP-selection behavior into shared logic and make providers opt into the same rules:

- filter by OTP send time when available
- avoid reusing the last successful mail
- prefer semantic OTP matches over any 6-digit match
- reject stale or obviously unrelated OpenAI-adjacent mail
- keep provider-specific fetch methods, but standardize candidate ranking

This should start with the biggest gap:

- move `Freemail` closer to the current `TempMail` safety model

If possible in the same pass, also align:

- `DuckMail`
- Outlook message ranking

### Stage 2: Registration Core Decomposition

Split `src/core/register.py` into internal units with stable boundaries, while preserving current behavior:

- authorize/bootstrap step
- email/OTP step
- account creation step
- token acquisition step
- persistence/result assembly step

The existing `RegistrationEngine.run()` contract can remain initially, but the implementation should stop being one giant control file.

The `packages/registration_core` layer should evolve from a bridge to a true orchestration boundary.

### Stage 3: Task Path Promotion

Promote the task path from “new side path” to “default execution path”:

- Web registration entry should create tasks via the migrated store/service path
- execution should prefer `apps.api.task_service` and `apps.worker`
- legacy route code should become compatibility shells

This reduces duplicate orchestration logic and makes behavior easier to reason about.

### Stage 4: Log Write Pressure Reduction

The current runtime writes a lot of log lines directly into storage during execution. That is acceptable for low volume but becomes expensive at higher task counts.

Introduce lighter write behavior:

- buffer task logs in memory per running task
- flush in small batches
- always flush on task completion or failure
- preserve user-visible log order

This should reduce database churn without losing debuggability.

### Stage 5: Heavy Flow Isolation

Separate the ordinary registration path from heavier browser/payment follow-up flows:

- payment bind flow
- browser-driven follow-up steps
- optional post-registration enrichment

The registration path should produce a clean result object and hand off any heavier downstream work explicitly, not inline by default.

## Architecture Direction

The target runtime model becomes:

```text
Web UI / compatibility routes
  -> task creation service
  -> migrated task store
  -> worker execution
  -> decomposed registration orchestration
  -> provider adapters with shared OTP policy
  -> persistence + structured result
  -> optional downstream heavy flows
```

This preserves compatibility while moving complexity away from UI-bound code.

## File-Level Direction

### Provider-side work

Primary files likely involved:

- `src/services/temp_mail.py`
- `src/services/freemail.py`
- `src/services/duck_mail.py`
- `src/services/outlook/service.py`
- `src/services/outlook/providers/graph_api.py`
- new shared helper file under `src/services/` or `packages/email_providers/`

Direction:

- shared OTP candidate normalization and ranking
- provider-specific mail fetch stays local
- provider-agnostic OTP decision logic moves out

### Registration-core work

Primary files likely involved:

- `src/core/register.py`
- `packages/registration_core/engine.py`
- new helper modules under `packages/registration_core/`

Direction:

- keep external engine contract stable first
- split internals by step and responsibility
- migrate call sites gradually

### Task-path work

Primary files likely involved:

- `apps/api/task_service.py`
- `apps/worker/main.py`
- `src/web/routes/registration.py`
- `packages/account_store/*`

Direction:

- reduce direct legacy execution entrypoints
- ensure Web UI launches tasks through the new path
- keep old endpoints alive as compatibility wrappers

### Log-pressure work

Primary files likely involved:

- `apps/worker/main.py`
- `src/core/register.py`
- `packages/account_store/logs.py`
- task-log related database helpers

Direction:

- add buffered logging
- flush on interval / threshold / finalization
- keep ordering deterministic

### Heavy-flow isolation work

Primary files likely involved:

- `src/web/routes/payment.py`
- `apps/api/payment_task_service.py`
- registration-to-payment bridge points

Direction:

- separate post-registration heavy actions from registration success path
- keep handoff explicit

## Risks

1. Hidden coupling in `src/core/register.py`
   Large files often hide ordering assumptions. Splitting too aggressively can cause behavioral drift.

2. Provider inconsistency
   Different providers expose different mail metadata quality, so the shared OTP model must tolerate missing timestamps and incomplete bodies.

3. Legacy/UI compatibility drift
   If task promotion happens too fast, the old UI could lose assumptions it still depends on.

4. Logging regressions
   Buffered logs can accidentally drop lines on crash unless completion/failure flush behavior is explicit.

5. Payment-path entanglement
   Registration and bind-card code already touch each other. Isolation needs a clean boundary or it will regress behavior.

## Test Strategy

This work should be driven by tests in layers:

### OTP policy tests

- stale mail ignored
- same old code not reused
- semantic OTP beats incidental 6-digit number
- missing timestamp behavior remains safe

### Registration-core tests

- orchestration step calls remain in correct order
- login/token fallback behavior remains intact
- result assembly remains compatible

### Task-path tests

- old route launches new task path
- worker runs task and flushes logs
- compatibility responses remain stable

### Logging tests

- buffered logs preserve order
- completion flush writes final lines
- failure flush still persists logs

### Regression tests

Re-run the current app/legacy bridge suites after each major slice.

## Rollout Order

Recommended implementation order:

1. provider OTP unification
2. logging buffer introduction
3. registration-core decomposition behind stable contract
4. task-path promotion in Web registration routes
5. heavy-flow isolation

This order improves success rate first, lowers pressure second, then reduces structural debt with lower operational risk.

## Acceptance Criteria

This refactor is successful when:

- `Freemail` and other selected providers stop reusing stale OTPs under regression tests
- task execution still passes existing bridge and worker tests
- Web UI registration still works through compatibility entrypoints
- high-frequency task runs reduce database write pressure measurably at the task-log layer
- registration success path no longer directly drags payment/browser-heavy follow-up logic by default
- code ownership is clearer than before, with less logic trapped in `src/core/register.py`

## Recommended Next Step

Write an implementation plan that breaks this into small TDD-driven tasks, with the first slice focused on provider OTP policy unification plus log-write pressure reduction.
