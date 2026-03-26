"""Minimum task persistence boundary for the phase 1 migration."""

from src.database import crud


class TaskStore:
    """Thin wrapper over the current registration task CRUD surface."""

    def __init__(self, db_manager):
        self._db_manager = db_manager

    def create(self, task_uuid: str, status: str = "pending", **kwargs):
        with self._db_manager.session_scope() as db:
            task = crud.create_registration_task(
                db,
                task_uuid=task_uuid,
                email_service_id=kwargs.get("email_service_id"),
                proxy=kwargs.get("proxy"),
            )
            if status != "pending":
                task = crud.update_registration_task(db, task_uuid, status=status)
            return task

    def get(self, task_uuid: str):
        with self._db_manager.session_scope() as db:
            return crud.get_registration_task_by_uuid(db, task_uuid)

    def update(self, task_uuid: str, **kwargs):
        with self._db_manager.session_scope() as db:
            return crud.update_registration_task(db, task_uuid, **kwargs)
