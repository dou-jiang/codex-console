# Phase 2 Quickstart

## What This Doc Is For

This repository is now a direct refactor line of `dou-jiang/codex-console`.

The legacy Web UI still exists, but phase 2 introduces a new task-driven path
that separates:

- API entry
- worker execution
- task storage
- task logs
- registration engine boundaries

If you only used the original project before, the main thing to know is:

the new path no longer assumes every execution has to be driven directly from
the old Web route layer.

One important update for the current branch:

- task API calls now require authenticated access
- browser callers can reuse the normal Web UI login cookie
- direct callers should provide `X-Access-Password`

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
python scripts/run_api.py --host 127.0.0.1 --port 8000 --database-url sqlite:///./data/api.db
```

The wrapper script can now be launched directly from the repository root without setting `PYTHONPATH` first.

Equivalent module form:

```bash
python -m apps.api.main --host 127.0.0.1 --port 8000 --database-url sqlite:///./data/api.db
```

## Create a Task

```http
POST /tasks/register
Content-Type: application/json
X-Access-Password: StrongPass123!

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
X-Access-Password: StrongPass123!
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
X-Access-Password: StrongPass123!
```

Returns:

- total task count
- basic summary for each task

## Read Task Logs

```http
GET /tasks/{task_uuid}/logs
X-Access-Password: StrongPass123!
```

Returns:

- task UUID
- ordered log lines

## Run a Specific Task

```http
POST /tasks/{task_uuid}/run
X-Access-Password: StrongPass123!
```

## Run the Next Pending Task

```http
POST /tasks/run-next
X-Access-Password: StrongPass123!
```

## Run the Worker Loop Directly

You can also drive the new worker path directly from Python without the API:

```bash
python scripts/run_worker.py --database-url sqlite:///./data/api.db --max-iterations 1 --poll-interval-seconds 1
```

The worker wrapper follows the same behavior and can also be started directly from the repository root.

Equivalent module form:

```bash
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
- run endpoints are still synchronous internal controls, so they should stay behind authenticated internal access
- legacy SQLAlchemy/deprecated datetime warnings still exist

## Recommended Next Step

The next practical move is to replace manual triggering with a thin background loop or external queue boundary, while keeping the same task/result contracts.
