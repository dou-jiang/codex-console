# Global List Style and Scheduled Summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify the site's list/table UI into a more professional card-style admin experience and replace scheduled-task raw summaries with concise business-readable summaries.

**Architecture:** Keep the change front-end only: upgrade the shared table/list design tokens in `static/css/style.css`, then layer scheduled-task-specific rendering logic in `static/js/scheduled_tasks.js`. Touch templates only where new semantic classes are needed so existing data flow and APIs remain unchanged.

**Tech Stack:** Jinja templates, vanilla JavaScript, shared CSS, pytest, existing front-end asset tests

---

## File Map

- `static/css/style.css` — global list/table visual system and scheduled-task detail styles.
- `static/js/scheduled_tasks.js` — concise summary formatting and richer detail rendering.
- `templates/scheduled_tasks.html` — semantic hooks for list toolbars/detail sections if needed.
- `tests/test_scheduled_tasks_page_assets.py` — summary and scheduled-task asset coverage.
- `tests/test_registration_page_assets.py` / related asset tests — guard existing page asset rendering if template hooks change.

## Task 1: Add failing tests for concise scheduled summaries

**Files:**
- Modify: `tests/test_scheduled_tasks_page_assets.py`
- Test: `tests/test_scheduled_tasks_page_assets.py`

- [ ] **Step 1: Write failing tests for summary formatting**

Add focused assertions covering:
- `cpa_refill` summary renders as `补号 X`
- `cpa_cleanup` summary renders as `检测 X · 清理 Y`
- `account_refresh` summary renders as `处理 X · 刷新 Y · 上传 Z`
- failed/cancelled summaries append a short `原因：...`

Example assertions to add:

```python
assert "补号 6" in rendered
assert "检测 20 · 清理 18" in rendered
assert "处理 30 · 刷新 28 · 上传 27" in rendered
assert "原因：接口超时" in rendered
```

- [ ] **Step 2: Run the focused asset tests to verify they fail**

Run:

```bash
pytest tests/test_scheduled_tasks_page_assets.py -q
```

Expected: FAIL because the current UI still renders raw summary JSON / generic text.

- [ ] **Step 3: Implement minimal summary formatter**

Update `static/js/scheduled_tasks.js` to:
- add task-specific summary formatting helpers
- normalize missing numeric fields to `0`
- append short failure/cancel reason text only when status indicates failure/cancellation

- [ ] **Step 4: Re-run the focused tests and make them pass**

Run:

```bash
pytest tests/test_scheduled_tasks_page_assets.py -q
```

Expected: PASS.

## Task 2: Upgrade scheduled-task detail/list rendering

**Files:**
- Modify: `static/js/scheduled_tasks.js`
- Modify: `templates/scheduled_tasks.html`
- Test: `tests/test_scheduled_tasks_page_assets.py`

- [ ] **Step 1: Write failing tests for the new scheduled-task detail structure**

Add tests asserting the rendered detail/log area includes:
- structured summary output rather than raw JSON
- summary/detail wrapper classes
- reusable action group / meta block hooks

Example assertions:

```python
assert "scheduled-run-summary" in script_or_markup
assert "scheduled-run-detail-grid" in script_or_markup
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run:

```bash
pytest tests/test_scheduled_tasks_page_assets.py -q
```

Expected: FAIL because those classes/rendered structures do not exist yet.

- [ ] **Step 3: Implement the minimal rendering changes**

Update scheduled-task rendering so:
- list action buttons use a consistent action-group container
- run detail modal renders summary as dedicated tags/text blocks
- status/meta area and log panel receive stable class names for styling

- [ ] **Step 4: Re-run the focused tests and make them pass**

Run:

```bash
pytest tests/test_scheduled_tasks_page_assets.py -q
```

Expected: PASS.

## Task 3: Upgrade global list/table styling

**Files:**
- Modify: `static/css/style.css`
- Modify: `templates/scheduled_tasks.html`
- Modify: `templates/accounts.html`
- Modify: `templates/email_services.html`
- Modify: `templates/settings.html`
- Modify: `templates/index.html`
- Test: `tests/test_scheduled_tasks_page_assets.py`
- Test: `tests/test_registration_page_assets.py`
- Test: `tests/test_settings_proxy_assets.py`

- [ ] **Step 1: Write/extend failing asset tests for shared list classes**

Add lightweight assertions that the shared templates reference new semantic wrappers/classes such as:
- shared list toolbar/header hooks
- shared action-group hook
- scheduled-task detail/list styling hooks

Example:

```python
assert "table-shell" in html
assert "table-actions" in html
```

- [ ] **Step 2: Run the relevant asset tests to verify they fail**

Run:

```bash
pytest tests/test_scheduled_tasks_page_assets.py tests/test_registration_page_assets.py tests/test_settings_proxy_assets.py -q
```

Expected: FAIL because the new shared classes/hooks are not present yet.

- [ ] **Step 3: Implement the minimal global styling changes**

Update `static/css/style.css` to:
- strengthen `.table-container`, `.data-table`, `.status-badge`, `.pagination`
- add reusable list-system classes (`.table-shell`, `.table-actions`, `.table-summary`, `.table-meta`, `.table-chip`, etc.)
- add scheduled-task detail/log panel styling that matches the global list system

Update templates only as needed to attach semantic classes without changing data flow.

- [ ] **Step 4: Re-run the relevant asset tests and make them pass**

Run:

```bash
pytest tests/test_scheduled_tasks_page_assets.py tests/test_registration_page_assets.py tests/test_settings_proxy_assets.py -q
```

Expected: PASS.

## Task 4: Run broader verification

**Files:**
- No additional code changes expected unless regressions appear.

- [ ] **Step 1: Run the broader front-end regression suite**

Run:

```bash
pytest tests/test_scheduled_tasks_page_assets.py tests/test_registration_page_assets.py tests/test_settings_proxy_assets.py tests/test_static_asset_versioning.py -q
```

Expected: PASS.

- [ ] **Step 2: Run any additional impacted route/template tests if needed**

Run:

```bash
pytest tests/test_scheduled_tasks_routes.py tests/test_proxy_settings_routes.py -q
```

Expected: PASS.

- [ ] **Step 3: Review the diff for accidental template/style regressions**

Run:

```bash
git diff -- static/css/style.css static/js/scheduled_tasks.js templates/
```

Expected: only the planned list-style and scheduled-summary changes.
