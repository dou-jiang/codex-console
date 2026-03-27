"""Minimum task-log persistence boundary for the phase 1 migration."""

from src.database import crud


class TaskLogStore:
    """Thin wrapper over task log append/read behavior."""

    def __init__(self, db_manager):
        self._db_manager = db_manager

    def append(self, task_uuid: str, message: str) -> bool:
        with self._db_manager.session_scope() as db:
            return crud.append_task_log(db, task_uuid, message)

    def append_many(self, task_uuid: str, messages: list[str]) -> bool:
        lines = [str(message) for message in messages if str(message or "").strip()]
        if not lines:
            return True

        with self._db_manager.session_scope() as db:
            task = crud.get_registration_task_by_uuid(db, task_uuid)
            if not task:
                return False

            existing = str(task.logs or "").splitlines() if task.logs else []
            task.logs = "\n".join([*existing, *lines]) if existing else "\n".join(lines)
            db.commit()
            return True

    def list(self, task_uuid: str) -> list[str]:
        with self._db_manager.session_scope() as db:
            task = crud.get_registration_task_by_uuid(db, task_uuid)
            if not task or not task.logs:
                return []
            return [line for line in str(task.logs).splitlines() if line]
