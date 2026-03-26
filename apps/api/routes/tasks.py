"""Minimal task routes for phase 2 API skeleton."""

from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel


router = APIRouter()


class RegisterTaskCreate(BaseModel):
    email_service_type: str
    proxy_url: str | None = None


@router.post("/tasks/register", status_code=202)
def create_register_task(payload: RegisterTaskCreate, request: Request):
    task_uuid = str(uuid4())
    task = request.app.state.store.tasks.create(
        task_uuid=task_uuid,
        status="pending",
        proxy=payload.proxy_url,
    )
    request_payload = {
        "email_service_type": payload.email_service_type,
        "proxy_url": payload.proxy_url,
    }
    task = request.app.state.store.tasks.update(task_uuid, result={"request": request_payload})
    return {
        "task_uuid": task.task_uuid,
        "status": task.status,
        "email_service_type": payload.email_service_type,
    }


@router.get("/tasks/{task_uuid}")
def get_register_task(task_uuid: str, request: Request):
    task = request.app.state.store.tasks.get(task_uuid)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")

    return {
        "task_uuid": task.task_uuid,
        "status": task.status,
        "logs": [line for line in str(task.logs or "").splitlines() if line],
        "result": task.result,
    }
