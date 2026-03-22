# Scheduled Task Config Editor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the scheduled-task config JSON textarea with a typed key/value editor plus an optional raw JSON advanced mode, while preserving backward compatibility and persisting per-key descriptions.

**Architecture:** Add a small persisted `config_meta` JSON field on scheduled plans so UI-only metadata (key/value descriptions and value types) survives edits without polluting runtime `config`. Keep the backend task runners using `config` only, extend the scheduled-plan API schemas/routes to round-trip `config_meta`, and rebuild the scheduled-tasks form around a front-end config-entry state model that can switch between table mode and raw JSON mode.

**Tech Stack:** FastAPI, SQLAlchemy ORM, SQLite/PostgreSQL migrations, vanilla JS, Jinja templates, pytest.

---

### Task 1: Persist config metadata on scheduled plans

**Files:**
- Modify: `src/database/models.py`
- Modify: `src/database/session.py`
- Modify: `src/database/crud.py`
- Modify: `src/scheduler/schemas.py`
- Modify: `src/web/routes/scheduled_tasks.py`
- Test: `tests/test_scheduler_data_model.py`
- Test: `tests/test_database_session.py`
- Test: `tests/test_scheduled_tasks_routes.py`

- [ ] **Step 1: Write failing tests**
- [ ] **Step 2: Run targeted tests to verify they fail**
- [ ] **Step 3: Add `config_meta` persistence and API round-trip**
- [ ] **Step 4: Re-run targeted tests to verify they pass**

### Task 2: Replace textarea with config editor UI

**Files:**
- Modify: `templates/scheduled_tasks.html`
- Modify: `static/js/scheduled_tasks.js`
- Test: `tests/test_scheduled_tasks_page_assets.py`

- [ ] **Step 1: Write failing asset tests for the new config editor hooks**
- [ ] **Step 2: Run targeted tests to verify they fail**
- [ ] **Step 3: Implement config-entry state, typed row rendering, mode switching, and payload serialization**
- [ ] **Step 4: Re-run targeted tests to verify they pass**

### Task 3: Full verification

**Files:**
- Modify: none
- Test: full suite

- [ ] **Step 1: Run the relevant targeted suite**
- [ ] **Step 2: Run `pytest -q`**
- [ ] **Step 3: Commit the finished change**
