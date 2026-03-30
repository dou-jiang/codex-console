import asyncio
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException

from src.web.routes import registration


@contextmanager
def _fake_get_db():
    yield object()


def _fake_task(task_uuid: str):
    return SimpleNamespace(
        id=1,
        task_uuid=task_uuid,
        status="pending",
        email_service_id=None,
        proxy=None,
        logs=None,
        result=None,
        error_message=None,
        created_at=None,
        started_at=None,
        completed_at=None,
    )


class BatchRegistrationLimitTests(unittest.TestCase):
    def test_batch_registration_accepts_1000(self):
        created_task_uuids = []

        def fake_create_registration_task(db, task_uuid, proxy=None):
            created_task_uuids.append(task_uuid)
            return _fake_task(task_uuid)

        request = registration.BatchRegistrationRequest(
            count=1000,
            email_service_type="tempmail",
            interval_min=0,
            interval_max=0,
            concurrency=1,
            mode="pipeline",
        )
        background_tasks = BackgroundTasks()

        with patch.object(registration, "get_db", _fake_get_db), \
             patch.object(registration.crud, "create_registration_task", side_effect=fake_create_registration_task), \
             patch.object(registration.crud, "get_registration_task", side_effect=lambda db, task_uuid: _fake_task(task_uuid)):
            response = asyncio.run(registration.start_batch_registration(request, background_tasks))

        self.assertEqual(response.count, 1000)
        self.assertEqual(len(response.tasks), 1000)
        self.assertEqual(len(created_task_uuids), 1000)
        self.assertEqual(len(background_tasks.tasks), 1)

    def test_batch_registration_rejects_over_1000(self):
        request = registration.BatchRegistrationRequest(
            count=1001,
            email_service_type="tempmail",
            interval_min=0,
            interval_max=0,
            concurrency=1,
            mode="pipeline",
        )

        with self.assertRaises(HTTPException) as exc_info:
            asyncio.run(registration.start_batch_registration(request, BackgroundTasks()))

        self.assertEqual(exc_info.exception.status_code, 400)
        self.assertIn("1-1000", exc_info.exception.detail)
