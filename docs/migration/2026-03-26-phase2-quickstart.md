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
python scripts/run_api.py --host 127.0.0.1 --port 8000 --database-url sqlite:///./data/api.db
```

Equivalent module form:

```bash
set PYTHONPATH=C:\path\to\repo
python -m apps.api.main --host 127.0.0.1 --port 8000 --database-url sqlite:///./data/api.db
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

## Run the Worker Loop Directly

You can also drive the new worker path directly from Python without the API:

```bash
set PYTHONPATH=C:\path\to\repo
python scripts/run_worker.py --database-url sqlite:///./data/api.db --max-iterations 1 --poll-interval-seconds 1
```

Equivalent module form:

```bash
set PYTHONPATH=C:\path\to\repo
python -m apps.worker.main --database-url sqlite:///./data/api.db --max-iterations 1 --poll-interval-seconds 1
```

Or from Python:

```python
from apps.worker.main import run_worker_loop

run_worker_loop(
    database_url="sqlite:///./data/api.db",
    max_iterations=1,
    poll_interval_seconds=1.0,
)
```

## Current Limits

- execution is still local and in-process
- there is no external queue broker yet
- task scheduling is still manual
- legacy SQLAlchemy/deprecated datetime warnings still exist

## Recommended Next Step

The next practical move is to replace manual triggering with a thin background loop or external queue boundary, while keeping the same task/result contracts.
