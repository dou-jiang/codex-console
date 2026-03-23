# Pagination Jump and Filter Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add unified jump-to-page controls for paginated list views and upgrade filter areas into reusable card-style filter panels.

**Architecture:** Keep the work mostly front-end: extend shared list styles in `static/css/style.css`, then wire page-specific controls in `accounts` and `scheduled_tasks` templates/JS. Reuse existing pagination state where present, and add only the minimum extra response handling needed for scheduled runs to compute total pages correctly.

**Tech Stack:** Jinja templates, vanilla JavaScript, shared CSS, pytest, existing Node-based front-end harness tests

---

## File Map

- `static/css/style.css` — shared filter panel and pagination jump styles.
- `templates/accounts.html` — account filter panel and pagination jump controls.
- `static/js/accounts.js` — account pagination jump behavior and validation.
- `templates/scheduled_tasks.html` — run-center filter panel and pagination controls.
- `static/js/scheduled_tasks.js` — run-center pagination state, jump behavior, and query updates.
- `src/web/routes/scheduled_tasks.py` — only if needed to return `total/page/page_size` for `/scheduled-runs`.
- `tests/test_registration_page_assets.py` — template hooks for shared pagination/filter UI if accounts template changes.
- `tests/test_scheduled_tasks_page_assets.py` — scheduled-task pagination/filter hook coverage and behavior tests.
- `tests/test_scheduled_tasks_routes.py` — route pagination metadata coverage if API changes are required.

## Task 1: Add failing tests for account pagination jump controls

**Files:**
- Modify: `tests/test_registration_page_assets.py`
- Modify: `templates/accounts.html`
- Modify: `static/js/accounts.js`

- [ ] **Step 1: Write failing asset/harness tests for account jump-to-page**

Add tests asserting:
- accounts template contains page input + jump button hooks
- `accounts.js` listens for Enter and button click
- out-of-range input is clamped with a lightweight notice path

Example assertions:

```python
assert 'id="page-jump-input"' in template
assert 'id="page-jump-btn"' in template
assert "keydown" in script and "Enter" in script
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run:

```bash
pytest tests/test_registration_page_assets.py -q
```

Expected: FAIL because accounts jump-to-page controls do not exist yet.

- [ ] **Step 3: Implement the minimal account jump behavior**

Update `templates/accounts.html` and `static/js/accounts.js` to:
- add jump input/button markup inside the pagination area
- parse and clamp requested pages
- support button click and Enter
- avoid duplicate loads when target page equals current page

- [ ] **Step 4: Re-run the focused tests and make them pass**

Run:

```bash
pytest tests/test_registration_page_assets.py -q
```

Expected: PASS.

## Task 2: Add failing tests for scheduled-run pagination controls

**Files:**
- Modify: `tests/test_scheduled_tasks_page_assets.py`
- Modify: `templates/scheduled_tasks.html`
- Modify: `static/js/scheduled_tasks.js`
- Modify: `src/web/routes/scheduled_tasks.py` (if needed)
- Modify: `tests/test_scheduled_tasks_routes.py` (if needed)

- [ ] **Step 1: Write failing tests for scheduled-run pagination UI and behavior**

Add tests covering:
- scheduled tasks template exposes run-center pagination hooks
- jump-to-page input/button update scheduled run query page
- Enter key triggers jump
- invalid/out-of-range values clamp with warning behavior
- if route metadata is needed, assert `/api/scheduled-runs` returns `total/page/page_size`

Example assertions:

```python
assert 'id="scheduled-run-page-jump-input"' in template
assert 'id="scheduled-run-prev-page"' in template
assert "?page=3&page_size=20" in requested_url
```

- [ ] **Step 2: Run the focused scheduled-run tests to verify they fail**

Run:

```bash
pytest tests/test_scheduled_tasks_page_assets.py tests/test_scheduled_tasks_routes.py -q
```

Expected: FAIL because the run-center pagination UI/behavior is missing.

- [ ] **Step 3: Implement minimal scheduled-run pagination**

Make these changes:
- add pagination controls under the run-center table
- store/update total/page/pageSize metadata from the API response
- wire prev/next/jump controls to `scheduledRunFilters.page`
- if necessary, extend `/scheduled-runs` route response with pagination metadata

- [ ] **Step 4: Re-run the focused tests and make them pass**

Run:

```bash
pytest tests/test_scheduled_tasks_page_assets.py tests/test_scheduled_tasks_routes.py -q
```

Expected: PASS.

## Task 3: Add failing tests for shared filter panel styling hooks

**Files:**
- Modify: `tests/test_registration_page_assets.py`
- Modify: `tests/test_scheduled_tasks_page_assets.py`
- Modify: `static/css/style.css`
- Modify: `templates/accounts.html`
- Modify: `templates/scheduled_tasks.html`

- [ ] **Step 1: Write failing tests for filter panel hooks**

Add assertions that:
- accounts filter area uses shared filter panel classes
- scheduled-run filter area uses shared filter panel classes
- shared stylesheet defines filter panel and pagination panel selectors

Example:

```python
assert "filter-panel" in template
assert ".filter-panel" in stylesheet
assert ".pagination-panel" in stylesheet
```

- [ ] **Step 2: Run the focused asset tests to verify they fail**

Run:

```bash
pytest tests/test_registration_page_assets.py tests/test_scheduled_tasks_page_assets.py -q
```

Expected: FAIL because the new filter/pagination shell classes do not exist yet.

- [ ] **Step 3: Implement the shared filter panel and pagination styling**

Update `static/css/style.css` to add:
- `.filter-panel`
- `.filter-panel-grid`
- `.filter-panel-actions`
- `.pagination-panel`
- `.pagination-jump`

Update templates to attach those classes cleanly without changing data behavior.

- [ ] **Step 4: Re-run the focused asset tests and make them pass**

Run:

```bash
pytest tests/test_registration_page_assets.py tests/test_scheduled_tasks_page_assets.py -q
```

Expected: PASS.

## Task 4: Run broader verification

**Files:**
- No additional code changes expected unless regressions are discovered.

- [ ] **Step 1: Run the impacted front-end and route suites**

Run:

```bash
pytest tests/test_registration_page_assets.py tests/test_scheduled_tasks_page_assets.py tests/test_scheduled_tasks_routes.py tests/test_static_asset_versioning.py -q
```

Expected: PASS.

- [ ] **Step 2: Run the full project suite**

Run:

```bash
pytest -q
```

Expected: PASS.

- [ ] **Step 3: Review the diff for scope control**

Run:

```bash
git diff -- templates/accounts.html templates/scheduled_tasks.html static/js/accounts.js static/js/scheduled_tasks.js static/css/style.css src/web/routes/scheduled_tasks.py tests/
```

Expected: only pagination/filter related changes plus matching tests/docs.
