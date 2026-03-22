# Scheduled Tasks UI Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the scheduled-tasks page so CPA service load failures are visible to users, repeated button clicks do not trigger duplicate work, and asset tests verify rendered row-button structure instead of only static hooks.

**Architecture:** Keep the fix page-local. Strengthen `static/js/scheduled_tasks.js` with a tiny click-guard/request-lock helper and explicit CPA-service load error reporting, then make rendered action buttons expose stable `data-*` markers. Back this with focused asset tests that fail on missing guard logic and missing rendered button structure.

**Tech Stack:** FastAPI templates, vanilla JS, pytest

---

## File Structure / Responsibility Map

### Modified files

- `static/js/scheduled_tasks.js` — add page-local click guard / request lock helper, CPA service load toast, and stable row-button render markers.
- `templates/scheduled_tasks.html` — add stable attributes for static page buttons where needed so the page markup and JS stay aligned.
- `tests/test_scheduled_tasks_page_assets.py` — add failing/passing coverage for toast handling, busy-state guard logic, and rendered row-button structure.

---

### Task 1: Strengthen scheduled-tasks asset expectations first

**Files:**
- Modify: `tests/test_scheduled_tasks_page_assets.py`

- [ ] **Step 1: Add a failing asset test for CPA service load error visibility**

```python
def test_scheduled_tasks_script_surfaces_cpa_service_load_failures_to_users():
    script = Path("static/js/scheduled_tasks.js").read_text(encoding="utf-8")
    assert "CPA 服务列表加载失败" in script
    assert "toast.error(" in script
```

- [ ] **Step 2: Add a failing asset test for button busy-state guard logic**

```python
def test_scheduled_tasks_script_contains_button_busy_guard_logic():
    script = Path("static/js/scheduled_tasks.js").read_text(encoding="utf-8")
    assert "data-busy" in script
    assert "disabled = true" in script
    assert "disabled = false" in script
```

- [ ] **Step 3: Add a failing asset test for rendered plan-row action markers**

```python
def test_scheduled_tasks_script_renders_plan_row_action_markers():
    script = Path("static/js/scheduled_tasks.js").read_text(encoding="utf-8")
    assert 'data-action="detail"' in script
    assert 'data-action="logs"' in script
    assert 'data-action="edit"' in script
    assert 'data-action="toggle"' in script
    assert 'data-action="run-now"' in script
    assert 'data-plan-id="${plan.id}"' in script
```

- [ ] **Step 4: Run the focused asset tests to verify they fail**

Run:
```bash
pytest tests/test_scheduled_tasks_page_assets.py -q
```
Expected: FAIL because the script does not yet expose the toast text, busy-state guard, or stable rendered row-button markers.

- [ ] **Step 5: Commit the failing test expectations**

```bash
git add tests/test_scheduled_tasks_page_assets.py
git commit -m "test: cover scheduled tasks ui hardening assets"
```

---

### Task 2: Implement page-local click guards and clearer row rendering

**Files:**
- Modify: `static/js/scheduled_tasks.js`
- Modify: `templates/scheduled_tasks.html`
- Test: `tests/test_scheduled_tasks_page_assets.py`

- [ ] **Step 1: Add a small page-local helper for button guarding**

Implement a helper that accepts a button element and an async callback, exits early when `data-busy="true"`, sets `button.dataset.busy = "true"`, sets `button.disabled = true`, awaits the callback, and restores the button state in `finally`.

```javascript
async function withButtonBusy(button, action) {
    if (!button) {
        return action();
    }
    if (button.dataset.busy === 'true') {
        return;
    }
    button.dataset.busy = 'true';
    button.disabled = true;
    try {
        return await action();
    } finally {
        delete button.dataset.busy;
        button.disabled = false;
    }
}
```

- [ ] **Step 2: Route existing button-triggered actions through the guard helper**

Update these paths to use `withButtonBusy(...)` instead of directly firing work:

- refresh button click handler
- create-plan button click handler
- form submit button path (alongside the existing submit guard)
- rendered row buttons for detail / logs / edit / toggle / run-now
- rendered run-log buttons that fetch log content

For row buttons, prefer stable `data-*` attributes plus a thin wrapper that reads `dataset.action`, `dataset.planId`, and optional `dataset.shouldEnable`.

- [ ] **Step 3: Surface CPA service load failures with a user-visible toast**

Update `loadCpaServices()` to accept an optional flag such as `showErrorToast = false` and emit:

```javascript
toast.error('CPA 服务列表加载失败，请手动输入服务 ID');
```

Use silent prefetch on page load, but pass `true` when opening create/edit modals.

- [ ] **Step 4: Add stable render markers for plan-row buttons**

In `renderPlans(...)`, make each action button render stable markers:

```javascript
data-action="detail"
data-action="logs"
data-action="edit"
data-action="toggle"
data-action="run-now"
data-plan-id="${plan.id}"
```

Also add `data-should-enable` for the toggle button.

- [ ] **Step 5: Add static button markers in the template if needed for consistency**

If the implementation benefits from stable selectors, add `data-action` markers to the top-level create/refresh buttons in `templates/scheduled_tasks.html`, while keeping IDs intact.

- [ ] **Step 6: Run focused asset verification**

Run:
```bash
pytest tests/test_scheduled_tasks_page_assets.py tests/test_registration_page_assets.py -q
```
Expected: PASS.

- [ ] **Step 7: Commit the UI hardening implementation**

```bash
git add static/js/scheduled_tasks.js templates/scheduled_tasks.html tests/test_scheduled_tasks_page_assets.py
git commit -m "feat: harden scheduled tasks page actions"
```

---

### Task 3: Re-verify scheduled-tasks behavior after the hardening pass

**Files:**
- Verify only: `static/js/scheduled_tasks.js`, `templates/scheduled_tasks.html`, `tests/test_scheduled_tasks_page_assets.py`

- [ ] **Step 1: Run the focused scheduled-tasks suites**

Run:
```bash
pytest tests/test_scheduled_tasks_page_assets.py tests/test_scheduled_tasks_routes.py tests/test_scheduler_engine.py -q
```
Expected: PASS.

- [ ] **Step 2: Run the full suite to catch regressions**

Run:
```bash
pytest -q
```
Expected: PASS.

- [ ] **Step 3: Manually inspect the final diff for scope control**

Run:
```bash
git diff --stat HEAD~2..HEAD
```
Expected: only the scheduled-tasks JS/template/tests and plan/spec docs changed for this hardening branch.

- [ ] **Step 4: Commit only if verification required follow-up changes**

If verification exposes no new issue, do not add a no-op commit.
