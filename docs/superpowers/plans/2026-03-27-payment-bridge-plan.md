# Payment Bridge Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bridge the highest-value payment/bind-card surfaces onto shared helpers so the old payment routes stop being the sole source of truth.

**Architecture:** Keep checkout generation and browser automation in place, but move bind-card task creation and read/write CRUD behind a small shared service layer. Bridge the old payment route endpoints onto that layer first, then only after that consider the deeper auto-bind and subscription-sync flows.

**Tech Stack:** Python 3.10+, FastAPI, SQLAlchemy, pytest

---

## Scope Order

### Task A

Create a shared bind-card task service for:

- create
- list
- open
- delete

### Task B

Bridge legacy payment CRUD-style endpoints to the new service layer.

### Task C

Bridge the smaller state-update endpoints:

- sync subscription
- mark user action

### Task D

Only then consider deeper local/third-party auto-bind flows if they still need direct route-owned logic.

## Out of Scope For This Cut

- Rewriting browser automation
- Replacing checkout generation logic
- Replacing payment providers

## Done Criteria

- old bind-card task CRUD endpoints reuse shared helpers
- tests exist for create/list/open/delete bridge behavior
- full regression suite remains green
