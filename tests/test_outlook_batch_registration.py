import asyncio

from src.web.routes import registration


def test_start_outlook_batch_registration_schedules_registration_type(monkeypatch):
    captured = {}

    def fake_schedule(background_tasks, coroutine_func, *args):
        captured["background_tasks"] = background_tasks
        captured["coroutine_func"] = coroutine_func
        captured["args"] = args

    monkeypatch.setattr(registration, "_schedule_async_job", fake_schedule)

    request = registration.OutlookBatchRegistrationRequest(
        service_ids=[1, 2, 3],
        skip_registered=False,
        registration_type="parent",
    )

    response = asyncio.run(registration._start_outlook_batch_registration_internal(request))

    try:
        assert response.to_register == 3
        assert response.service_ids == [1, 2, 3]
        assert captured["coroutine_func"] is registration.run_outlook_batch_registration
        assert captured["args"][-1] == "parent"
    finally:
        registration.batch_tasks.pop(response.batch_id, None)


def test_run_outlook_batch_registration_passes_registration_type(monkeypatch):
    created_service_ids = []
    captured = {}

    class DummyDb:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_get_db():
        return DummyDb()

    def fake_create_registration_task(db, task_uuid, proxy, email_service_id):
        created_service_ids.append(email_service_id)

    async def fake_run_batch_registration(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(registration, "get_db", fake_get_db)
    monkeypatch.setattr(registration.crud, "create_registration_task", fake_create_registration_task)
    monkeypatch.setattr(registration, "run_batch_registration", fake_run_batch_registration)

    asyncio.run(
        registration.run_outlook_batch_registration(
            batch_id="batch-1",
            service_ids=[1, 2, 3],
            skip_registered=False,
            proxy=None,
            interval_min=5,
            interval_max=30,
            concurrency=2,
            mode="pipeline",
            registration_type="parent",
        )
    )

    assert created_service_ids == [1, 2, 3]
    assert captured["email_service_type"] == "outlook"
    assert captured["registration_type"] == "parent"


def test_resolve_task_email_service_id_prefers_task_binding(monkeypatch):
    class DummyDb:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class DummyTask:
        email_service_id = 42

    monkeypatch.setattr(registration, "get_db", lambda: DummyDb())
    monkeypatch.setattr(registration.crud, "get_registration_task", lambda db, task_uuid: DummyTask())

    assert registration._resolve_task_email_service_id("task-1", None) == 42


def test_run_batch_pipeline_uses_task_bound_email_service_ids(monkeypatch):
    captured = []

    class DummyDb:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class DummyTask:
        status = "completed"
        error_message = None

    async def fake_run_registration_task(
        task_uuid,
        email_service_type,
        proxy,
        email_service_config,
        email_service_id=None,
        **kwargs,
    ):
        captured.append((task_uuid, email_service_id))

    monkeypatch.setattr(registration, "run_registration_task", fake_run_registration_task)
    monkeypatch.setattr(
        registration,
        "_resolve_task_email_service_id",
        lambda task_uuid, fallback: {"task-1": 101, "task-2": 202}[task_uuid],
    )
    monkeypatch.setattr(registration, "get_db", lambda: DummyDb())
    monkeypatch.setattr(registration.crud, "get_registration_task", lambda db, task_uuid: DummyTask())
    monkeypatch.setattr(registration.task_manager, "init_batch", lambda batch_id, total: None)
    monkeypatch.setattr(registration.task_manager, "add_batch_log", lambda batch_id, message: None)
    monkeypatch.setattr(registration.task_manager, "update_batch_status", lambda batch_id, **kwargs: None)
    monkeypatch.setattr(registration.task_manager, "is_batch_cancelled", lambda batch_id: False)
    monkeypatch.setattr(registration.task_manager, "cancel_task", lambda task_uuid: None)
    monkeypatch.setattr(registration.task_manager, "update_status", lambda task_uuid, status, error=None: None)
    monkeypatch.setattr(registration.random, "randint", lambda a, b: 0)

    asyncio.run(
        registration.run_batch_pipeline(
            batch_id="batch-pipeline",
            task_uuids=["task-1", "task-2"],
            email_service_type="outlook",
            proxy=None,
            email_service_config=None,
            email_service_id=None,
            interval_min=0,
            interval_max=0,
            concurrency=1,
            registration_type="child",
        )
    )

    try:
        assert captured == [("task-1", 101), ("task-2", 202)]
    finally:
        registration.batch_tasks.pop("batch-pipeline", None)
