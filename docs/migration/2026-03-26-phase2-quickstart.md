# Phase 2 Quickstart

## What Exists Now

The extracted path can now do the following:

- create a registration task
- list tasks
- inspect a single task
- inspect task logs
- run a specific task
- run the next pending task

This is still a local in-process flow, not a full external queue yet.

## Main Entry Point

- API app: `apps/api/main.py`
- Worker runner: `apps/worker/main.py`

## Start the API

Example:

```bash
set PYTHONPATH=C:\path\to\repo
uvicorn apps.api.main:app --reload
```

## Create a Task

```http
POST /tasks/register
Content-Type: application/json

{
  "email_service_type": "duck_mail",
  "proxy_url": "http://127.0.0.1:8080",
  "email_service_config": {
    "base_url": "https://mail.example.test",
    "default_domain": "example.test"
  }
}
```

## Query a Task

```http
GET /tasks/{task_uuid}
```

Returns:

- task status
- proxy
- error message
- task logs
- execution result payload

## List Tasks

```http
GET /tasks
```

Returns:

- total task count
- basic summary for each task

## Read Task Logs

```http
GET /tasks/{task_uuid}/logs
```

Returns:

- task UUID
- ordered log lines

## Run a Specific Task

```http
POST /tasks/{task_uuid}/run
```

## Run the Next Pending Task

```http
POST /tasks/run-next
```

## Current Limits

- execution is still local and in-process
- there is no external queue broker yet
- task scheduling is still manual
- legacy SQLAlchemy/deprecated datetime warnings still exist

## Recommended Next Step

The next practical move is to replace manual triggering with a thin background loop or external queue boundary, while keeping the same task/result contracts.
