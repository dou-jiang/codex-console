"""Helpers to keep task API responses structurally consistent."""


def serialize_task(task, include_logs: bool = False, include_result: bool = False) -> dict:
    payload = {
        "task_uuid": task.task_uuid,
        "status": task.status,
        "error_message": str(task.error_message or ""),
        "proxy": task.proxy,
    }
    if include_logs:
        payload["logs"] = [line for line in str(task.logs or "").splitlines() if line]
    if include_result:
        payload["result"] = task.result
    return payload


def serialize_outcome(outcome: dict) -> dict:
    return {
        "success": bool(outcome.get("success", False)),
        "status": str(outcome.get("status", "") or ""),
        "error": str(outcome.get("error", "") or ""),
        "task_uuid": str(outcome.get("task_uuid", "") or ""),
    }
