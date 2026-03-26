# Payment Bind Bridge Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Start migrating the frozen payment/bind-card area by extracting the bind-card task CRUD surfaces behind a shared service layer.

**Architecture:** Keep checkout generation and browser automation in place for now, but stop letting the payment route own all bind-card task persistence and CRUD logic. First bridge create/list/open/delete behind a shared service, then revisit the deeper sync and auto-bind flows.

**Tech Stack:** Python 3.10+, FastAPI, SQLAlchemy, pytest

---

## Execution Order

### Task A

Add a shared bind-card task service for:

- create task
- list tasks
- open task
- delete task

### Task B

Bridge legacy payment endpoints to that service:

- `POST /bind-card/tasks`
- `GET /bind-card/tasks`
- `POST /bind-card/tasks/{task_id}/open`
- `DELETE /bind-card/tasks/{task_id}`

### Task C

Only after CRUD bridge is stable:

- evaluate `sync_bind_card_task_subscription`
- evaluate `mark_bind_card_task_user_action`
- evaluate `auto_bind_bind_card_task_third_party`
- evaluate `auto_bind_bind_card_task_local`

## Done Criteria

- bind-card task CRUD no longer lives entirely inside the route handlers
- old route behavior stays compatible
- tests cover the bridge points
