# Phase 1 Freeze List

## Purpose

Lock the phase 1 migration scope so registration-core extraction does not expand into unrelated browser or UI work.

## Frozen Areas

- `src/core/openai/browser_bind.py`
  - Reason: browser-heavy bind-card flow is not required to prove the extracted registration backbone
- `src/core/openai/payment.py`
  - Reason: payment and checkout concerns are outside the phase 1 registration-core scope
- `src/web/routes/payment.py`
  - Reason: route logic for payment depends on the browser/payment layer and would expand scope immediately
- `templates/`
  - Reason: template migration should wait until the new backend boundaries are stable
- `static/`
  - Reason: frontend asset migration is phase 3 work, not registration-core extraction work

## Unfreeze Condition

Only unfreeze these areas after:

- the extracted registration engine runs through the new package path
- the minimum mailbox/provider boundary is stable
- the minimum persistence boundary is stable
- the phase 1 regression suite stays green
