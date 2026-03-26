"""Minimal task routes for phase 2 API skeleton."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from apps.api.serializers import serialize_outcome, serialize_task
from apps.api.task_service import create_register_task_record
from apps.worker.main import WorkerRunner

router = APIRouter()


class RegisterTaskCreate(BaseModel):
    email_service_type: str
    proxy_url: str | None = None
    email_service_config: dict | None = None


@router.post("/tasks/register", status_code=202)
def create_register_task(payload: RegisterTaskCreate, request: Request):
    task = create_register_task_record(
        request.app.state.store,
        email_service_type=payload.email_service_type,
        proxy_url=payload.proxy_url,
        email_service_config=payload.email_service_config,
    )
    serialized = serialize_task(task, include_result=True)
    return {
        **serialized,
        "email_service_type": payload.email_service_type,
        "task": serialized,
    }


@router.get("/tasks/{task_uuid}")
def get_register_task(task_uuid: str, request: Request):
    task = request.app.state.store.tasks.get(task_uuid)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")

    serialized = serialize_task(task, include_logs=True, include_result=True)
    return {
        **serialized,
        "task": serialized,
    }


@router.get("/tasks/{task_uuid}/logs")
def get_task_logs(task_uuid: str, request: Request):
    task = request.app.state.store.tasks.get(task_uuid)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")

    return {
        "task_uuid": task.task_uuid,
        "logs": request.app.state.store.logs.list(task_uuid),
    }


@router.get("/tasks")
def list_register_tasks(request: Request):
    tasks = request.app.state.store.tasks.list(limit=100)
    return {
        "total": len(tasks),
        "items": [serialize_task(task) for task in tasks],
    }


@router.post("/tasks/{task_uuid}/run")
def run_register_task(task_uuid: str, request: Request):
    runner = WorkerRunner(request.app.state.store)
    outcome = runner.process_task(task_uuid)
    if outcome.get("error") == "task not found":
        raise HTTPException(status_code=404, detail="task not found")
    task = request.app.state.store.tasks.get(task_uuid)
    serialized_task = serialize_task(task, include_logs=True, include_result=True) if task else None
    serialized_outcome = serialize_outcome(outcome)
    return {
        **serialized_outcome,
        "task": serialized_task,
        "outcome": serialized_outcome,
    }


@router.post("/tasks/run-next")
def run_next_pending_task(request: Request):
    runner = WorkerRunner(request.app.state.store)
    outcome = runner.process_next_pending()
    if outcome.get("error") == "no pending tasks":
        raise HTTPException(status_code=404, detail="no pending tasks")
    task_uuid = str(outcome.get("task_uuid") or "")
    task = request.app.state.store.tasks.get(task_uuid) if task_uuid else None
    serialized_task = serialize_task(task, include_logs=True, include_result=True) if task else None
    serialized_outcome = serialize_outcome(outcome)
    return {
        **serialized_outcome,
        "task": serialized_task,
        "outcome": serialized_outcome,
    }
