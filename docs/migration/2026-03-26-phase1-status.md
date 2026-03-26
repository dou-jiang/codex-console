# Phase 1 Status

## Status

Phase 1 core extraction baseline is in a good state.

## Landed Boundaries

- `packages/registration_core`
  - stable input/output contracts
  - client boundary facade
  - new registration entrypoint that bridges to the legacy engine
- `packages/email_providers`
  - provider base export
  - factory facade
  - first adapter exports for `tempmail`, `temp_mail`, `duck_mail`
- `packages/account_store`
  - minimum persistence entrypoint
  - account create wrapper
  - registration task create/update wrapper
  - task log append/list wrapper

## Frozen Areas

The following remain intentionally untouched in phase 1:

- `src/core/openai/browser_bind.py`
- `src/core/openai/payment.py`
- `src/web/routes/payment.py`
- `templates/`
- `static/`

See also:

- `docs/migration/2026-03-26-phase1-freeze-list.md`

## Verification Run

The following suite passed together in the isolated worktree:

```text
tests/unit/registration_core/test_imports.py
tests/unit/registration_core/test_models.py
tests/unit/registration_core/test_client_boundary.py
tests/unit/registration_core/test_engine.py
tests/unit/email_providers/test_factory.py
tests/unit/email_providers/test_adapter_exports.py
tests/unit/account_store/test_accounts_store.py
tests/unit/account_store/test_task_log_store.py
tests/integration/test_registration_without_web_execution.py
tests/test_registration_engine.py
tests/test_temp_mail_service.py
tests/test_duck_mail_service.py
```

Result:

- `23 passed`

## Known Warnings

- SQLAlchemy deprecation warning around `declarative_base()`
- `datetime.utcnow()` deprecation warnings in the legacy persistence layer

These are not phase-1 blockers, but they should be cleaned up in a later maintenance pass.

## Next Recommended Step

Start phase 2 only after deciding whether to:

- keep the legacy `src.web` registration route as a thin compatibility shell
- or begin introducing `apps/api` and `apps/worker` as the new task-driven execution path
