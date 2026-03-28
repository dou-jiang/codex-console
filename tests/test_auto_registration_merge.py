import asyncio
from contextlib import contextmanager

from src.config.settings import Settings
from src.core import auto_registration
from src.core.auto_registration import AutoRegistrationPlan
from src.web.task_manager import task_manager
from src.web.routes import registration


def test_settings_exposes_auto_registration_fields():
    settings = Settings()

    assert settings.registration_auto_enabled is False
    assert settings.registration_auto_check_interval == 60
    assert settings.registration_auto_email_service_type == "tempmail"
    assert settings.registration_auto_mode == "pipeline"


def test_run_auto_registration_batch_exists():
    assert callable(registration.run_auto_registration_batch)


def test_run_auto_registration_batch_rejects_invalid_email_type():
    plan = AutoRegistrationPlan(
        deficit=1,
        ready_count=0,
        min_ready_auth_files=1,
        cpa_service_id=123,
    )
    settings = Settings(registration_auto_email_service_type="invalid-service")

    try:
        import asyncio

        asyncio.run(registration.run_auto_registration_batch(plan, settings))
    except ValueError as exc:
        assert "邮箱服务类型无效" in str(exc)
    else:
        raise AssertionError("expected ValueError for invalid email service type")


def test_build_auto_registration_plan_keeps_cpa_service_id(monkeypatch):
    settings = Settings(
        registration_auto_enabled=True,
        registration_auto_cpa_service_id=321,
        registration_auto_min_ready_auth_files=3,
    )

    monkeypatch.setattr(
        auto_registration,
        "get_auto_registration_inventory",
        lambda current_settings: (1, 3, 2),
    )

    plan = auto_registration.build_auto_registration_plan(settings)

    assert plan is not None
    assert plan.cpa_service_id == 321
    assert plan.ready_count == 1
    assert plan.min_ready_auth_files == 3
    assert plan.deficit == 2


def test_auto_registration_immediate_check_keeps_regular_interval(monkeypatch):
    class MutableSettings:
        registration_auto_enabled = False
        registration_auto_check_interval = 5
        registration_auto_min_ready_auth_files = 1

    settings = MutableSettings()
    plan_calls = []

    def fake_plan_builder(current_settings):
        plan_calls.append(current_settings.registration_auto_enabled)
        return AutoRegistrationPlan(
            deficit=0,
            ready_count=1,
            min_ready_auth_files=1,
            cpa_service_id=1,
        )

    async def fake_trigger_callback(plan, current_settings):
        return None

    monkeypatch.setattr(auto_registration, "add_auto_registration_log", lambda message: None)

    async def scenario():
        coordinator = auto_registration.AutoRegistrationCoordinator(
            trigger_callback=fake_trigger_callback,
            settings_getter=lambda: settings,
            plan_builder=fake_plan_builder,
        )

        coordinator.start()
        try:
            await asyncio.sleep(0.1)
            settings.registration_auto_enabled = True
            coordinator.request_immediate_check()
            await asyncio.sleep(5.5)
        finally:
            await coordinator.stop()

    asyncio.run(scenario())

    assert len(plan_calls) >= 2
    assert auto_registration.get_auto_registration_state()["last_checked_at"] is not None


def test_cancel_batch_tasks_marks_all_batch_tasks_cancelled(monkeypatch):
    batch_id = "batch-auto-cancel"
    task_uuids = ["task-1", "task-2"]
    registration.batch_tasks[batch_id] = {
        "task_uuids": task_uuids,
        "cancelled": False,
        "finished": False,
    }

    auto_registration.update_auto_registration_state(
        current_batch_id=batch_id,
        status="running",
        message="自动补货任务运行中",
    )

    log_messages = []
    monkeypatch.setattr(registration, "add_auto_registration_log", log_messages.append)

    for task_uuid in task_uuids:
        task_manager.cleanup_task(task_uuid)

    registration._cancel_batch_tasks(batch_id)

    for task_uuid in task_uuids:
        assert task_manager.is_cancelled(task_uuid) is True
        task_manager.cleanup_task(task_uuid)

    state = auto_registration.get_auto_registration_state()
    assert state["status"] == "cancelling"
    assert batch_id in state["message"]
    assert any(batch_id in message for message in log_messages)


def test_auto_registration_running_batch_can_be_cancelled(monkeypatch):
    plan = AutoRegistrationPlan(
        deficit=2,
        ready_count=0,
        min_ready_auth_files=2,
        cpa_service_id=123,
    )
    settings = Settings(
        registration_auto_email_service_type="tempmail",
        registration_auto_mode="pipeline",
        registration_auto_interval_min=1,
        registration_auto_interval_max=1,
        registration_auto_concurrency=1,
    )

    created_tasks = []
    batch_started = asyncio.Event()

    @contextmanager
    def fake_get_db():
        yield object()

    def fake_create_registration_task(db, task_uuid, proxy=None, email_service_id=None):
        created_tasks.append(task_uuid)
        return {"task_uuid": task_uuid}

    async def fake_run_batch_registration(**kwargs):
        batch_id = kwargs["batch_id"]
        task_uuids = kwargs["task_uuids"]
        registration._init_batch_state(batch_id, task_uuids)
        batch_started.set()

        while not all(task_manager.is_cancelled(task_uuid) for task_uuid in task_uuids):
            await asyncio.sleep(0.05)

        registration.batch_tasks[batch_id]["cancelled"] = True
        registration.batch_tasks[batch_id]["finished"] = True

    monkeypatch.setattr(registration, "get_db", fake_get_db)
    monkeypatch.setattr(registration.crud, "create_registration_task", fake_create_registration_task)
    monkeypatch.setattr(registration, "run_batch_registration", fake_run_batch_registration)
    monkeypatch.setattr(registration, "add_auto_registration_log", lambda message: None)

    async def scenario():
        worker = asyncio.create_task(registration.run_auto_registration_batch(plan, settings))
        await asyncio.wait_for(batch_started.wait(), timeout=2)

        batch_id = auto_registration.get_auto_registration_state()["current_batch_id"]
        assert batch_id

        response = await registration.cancel_batch(batch_id)
        assert response["success"] is True

        finished_batch_id = await asyncio.wait_for(worker, timeout=2)
        assert finished_batch_id == batch_id

        return batch_id

    batch_id = asyncio.run(scenario())

    assert len(created_tasks) == 2
    assert batch_id in registration.batch_tasks
    assert registration.batch_tasks[batch_id]["cancelled"] is True
    assert registration.batch_tasks[batch_id]["finished"] is True
    assert all(task_manager.is_cancelled(task_uuid) for task_uuid in created_tasks)

    state = auto_registration.get_auto_registration_state()
    assert state["status"] == "cancelled"
    assert batch_id in state["message"]

    for task_uuid in created_tasks:
        task_manager.cleanup_task(task_uuid)


def test_auto_registration_batch_refreshes_monitor_state_after_completion(monkeypatch):
    plan = AutoRegistrationPlan(
        deficit=2,
        ready_count=1,
        min_ready_auth_files=3,
        cpa_service_id=123,
    )
    settings = Settings(
        registration_auto_email_service_type="tempmail",
        registration_auto_mode="pipeline",
        registration_auto_min_ready_auth_files=3,
    )

    @contextmanager
    def fake_get_db():
        yield object()

    def fake_create_registration_task(db, task_uuid, proxy=None, email_service_id=None):
        return {"task_uuid": task_uuid}

    async def fake_run_batch_registration(**kwargs):
        batch_id = kwargs["batch_id"]
        registration._init_batch_state(batch_id, kwargs["task_uuids"])
        registration.batch_tasks[batch_id]["success"] = 2
        registration.batch_tasks[batch_id]["failed"] = 0
        registration.batch_tasks[batch_id]["finished"] = True

    monkeypatch.setattr(registration, "get_db", fake_get_db)
    monkeypatch.setattr(registration.crud, "create_registration_task", fake_create_registration_task)
    monkeypatch.setattr(registration, "run_batch_registration", fake_run_batch_registration)
    monkeypatch.setattr(registration, "add_auto_registration_log", lambda message: None)
    monkeypatch.setattr(
        registration,
        "get_auto_registration_inventory",
        lambda current_settings: (3, 3, 0),
    )

    asyncio.run(registration.run_auto_registration_batch(plan, settings))

    state = auto_registration.get_auto_registration_state()
    assert state["status"] == "idle"
    assert state["current_ready_count"] == 3
    assert state["target_ready_count"] == 3
    assert state["last_checked_at"] is not None


def test_auto_registration_batch_refresh_preserves_inventory_when_refresh_fails(monkeypatch):
    plan = AutoRegistrationPlan(
        deficit=1,
        ready_count=1,
        min_ready_auth_files=3,
        cpa_service_id=123,
    )
    settings = Settings(
        registration_auto_email_service_type="tempmail",
        registration_auto_mode="pipeline",
        registration_auto_min_ready_auth_files=3,
    )

    @contextmanager
    def fake_get_db():
        yield object()

    def fake_create_registration_task(db, task_uuid, proxy=None, email_service_id=None):
        return {"task_uuid": task_uuid}

    async def fake_run_batch_registration(**kwargs):
        batch_id = kwargs["batch_id"]
        registration._init_batch_state(batch_id, kwargs["task_uuids"])
        registration.batch_tasks[batch_id]["success"] = 1
        registration.batch_tasks[batch_id]["failed"] = 0
        registration.batch_tasks[batch_id]["finished"] = True

    monkeypatch.setattr(registration, "get_db", fake_get_db)
    monkeypatch.setattr(registration.crud, "create_registration_task", fake_create_registration_task)
    monkeypatch.setattr(registration, "run_batch_registration", fake_run_batch_registration)
    monkeypatch.setattr(registration, "add_auto_registration_log", lambda message: None)
    monkeypatch.setattr(registration, "get_auto_registration_inventory", lambda current_settings: None)

    auto_registration.update_auto_registration_state(current_ready_count=2, target_ready_count=3)

    asyncio.run(registration.run_auto_registration_batch(plan, settings))

    state = auto_registration.get_auto_registration_state()
    assert state["status"] == "idle"
    assert state["current_ready_count"] == 2
    assert state["target_ready_count"] == 3
    assert state["last_checked_at"] is not None


def test_cancelled_registration_task_persists_cancelled_status(monkeypatch):
    task_uuid = "cancelled-task-uuid"
    updates = []

    @contextmanager
    def fake_get_db():
        class FakeDb:
            def query(self, *args, **kwargs):
                class Query:
                    def filter(self, *filter_args, **filter_kwargs):
                        return self

                    def order_by(self, *order_args, **order_kwargs):
                        return self

                    def first(self):
                        return None

                    def all(self):
                        return []

                return Query()

        yield FakeDb()

    class FakeEngine:
        def __init__(self, **kwargs):
            self._cancel_requested = kwargs["cancel_requested"]

        def run(self):
            task_manager.cancel_task(task_uuid)
            assert self._cancel_requested() is True

            class Result:
                success = False
                error_message = "任务已取消"

            return Result()

    def fake_update_registration_task(db, current_task_uuid, **kwargs):
        updates.append((current_task_uuid, kwargs))
        return {"task_uuid": current_task_uuid, **kwargs}

    monkeypatch.setattr(registration, "get_db", fake_get_db)
    monkeypatch.setattr(registration.crud, "update_registration_task", fake_update_registration_task)
    monkeypatch.setattr(registration, "RegistrationEngine", FakeEngine)
    monkeypatch.setattr(registration, "get_settings", lambda: Settings())

    task_manager.cleanup_task(task_uuid)

    registration._run_sync_registration_task(
        task_uuid=task_uuid,
        email_service_type="tempmail",
        proxy=None,
        email_service_config={},
    )

    assert any(kwargs.get("status") == "running" for _, kwargs in updates)
    assert any(kwargs.get("status") == "cancelled" for _, kwargs in updates)
    assert not any(kwargs.get("status") == "failed" for _, kwargs in updates)

    task_manager.cleanup_task(task_uuid)


def test_parallel_batch_cancel_prevents_queued_tasks_from_starting(monkeypatch):
    batch_id = "parallel-cancel-batch"
    task_uuids = ["parallel-task-1", "parallel-task-2", "parallel-task-3"]
    started = []
    statuses = {task_uuid: "pending" for task_uuid in task_uuids}
    release_first = asyncio.Event()

    @contextmanager
    def fake_get_db():
        class FakeTask:
            def __init__(self, task_uuid):
                self.status = statuses[task_uuid]
                self.error_message = ""

        class FakeDb:
            pass

        yield FakeDb()

    async def fake_run_registration_task(task_uuid, *args, **kwargs):
        started.append(task_uuid)
        statuses[task_uuid] = "running"
        if task_uuid == task_uuids[0]:
            await release_first.wait()
            statuses[task_uuid] = "cancelled"
        else:
            statuses[task_uuid] = "completed"

    def fake_get_registration_task(db, task_uuid):
        class FakeTask:
            def __init__(self, status):
                self.status = status
                self.error_message = ""

        return FakeTask(statuses[task_uuid])

    def fake_update_registration_task(db, task_uuid, **kwargs):
        statuses[task_uuid] = kwargs.get("status", statuses[task_uuid])
        return None

    monkeypatch.setattr(registration, "get_db", fake_get_db)
    monkeypatch.setattr(registration, "run_registration_task", fake_run_registration_task)
    monkeypatch.setattr(registration.crud, "get_registration_task", fake_get_registration_task)
    monkeypatch.setattr(registration.crud, "update_registration_task", fake_update_registration_task)

    async def scenario():
        worker = asyncio.create_task(
            registration.run_batch_parallel(
                batch_id=batch_id,
                task_uuids=task_uuids,
                email_service_type="tempmail",
                proxy=None,
                email_service_config=None,
                email_service_id=None,
                concurrency=1,
            )
        )

        while not started:
            await asyncio.sleep(0.05)

        await registration.cancel_batch(batch_id)
        release_first.set()
        await asyncio.wait_for(worker, timeout=2)

    asyncio.run(scenario())

    assert started == [task_uuids[0]]
    assert statuses[task_uuids[1]] == "cancelled"
    assert statuses[task_uuids[2]] == "cancelled"

    for task_uuid in task_uuids:
        task_manager.cleanup_task(task_uuid)


def test_pipeline_wait_can_be_interrupted_by_cancel(monkeypatch):
    batch_id = "pipeline-cancel-batch"
    task_uuids = ["pipeline-task-1", "pipeline-task-2"]
    started = []

    async def fake_run_registration_task(task_uuid, *args, **kwargs):
        started.append(task_uuid)

    monkeypatch.setattr(registration, "run_registration_task", fake_run_registration_task)

    async def scenario():
        worker = asyncio.create_task(
            registration.run_batch_pipeline(
                batch_id=batch_id,
                task_uuids=task_uuids,
                email_service_type="tempmail",
                proxy=None,
                email_service_config=None,
                email_service_id=None,
                interval_min=5,
                interval_max=5,
                concurrency=1,
            )
        )

        while not started:
            await asyncio.sleep(0.05)

        await asyncio.sleep(0.2)
        await registration.cancel_batch(batch_id)
        await asyncio.wait_for(worker, timeout=2)

    asyncio.run(scenario())

    assert started == [task_uuids[0]]

    for task_uuid in task_uuids:
        task_manager.cleanup_task(task_uuid)


def test_pipeline_batch_updates_progress_for_completed_tasks(monkeypatch):
    batch_id = "pipeline-progress-batch"
    task_uuids = ["pipeline-progress-1", "pipeline-progress-2"]
    statuses = {task_uuid: "pending" for task_uuid in task_uuids}

    @contextmanager
    def fake_get_db():
        class FakeDb:
            pass

        yield FakeDb()

    async def fake_run_registration_task(task_uuid, *args, **kwargs):
        statuses[task_uuid] = "completed"

    def fake_get_registration_task(db, task_uuid):
        class FakeTask:
            def __init__(self, status):
                self.status = status
                self.error_message = ""

        return FakeTask(statuses[task_uuid])

    monkeypatch.setattr(registration, "get_db", fake_get_db)
    monkeypatch.setattr(registration, "run_registration_task", fake_run_registration_task)
    monkeypatch.setattr(registration.crud, "get_registration_task", fake_get_registration_task)

    asyncio.run(
        registration.run_batch_pipeline(
            batch_id=batch_id,
            task_uuids=task_uuids,
            email_service_type="tempmail",
            proxy=None,
            email_service_config=None,
            email_service_id=None,
            interval_min=0,
            interval_max=0,
            concurrency=1,
        )
    )

    batch = registration.batch_tasks[batch_id]
    assert batch["completed"] == 2
    assert batch["success"] == 2
    assert batch["failed"] == 0
    assert batch["finished"] is True

    for task_uuid in task_uuids:
        task_manager.cleanup_task(task_uuid)


def test_parallel_cancelled_tasks_contribute_to_completed_progress(monkeypatch):
    batch_id = "parallel-progress-cancel-batch"
    task_uuids = ["parallel-progress-1", "parallel-progress-2", "parallel-progress-3"]
    started = []
    statuses = {task_uuid: "pending" for task_uuid in task_uuids}
    release_first = asyncio.Event()

    @contextmanager
    def fake_get_db():
        class FakeDb:
            pass

        yield FakeDb()

    async def fake_run_registration_task(task_uuid, *args, **kwargs):
        started.append(task_uuid)
        statuses[task_uuid] = "running"
        await release_first.wait()
        statuses[task_uuid] = "cancelled"

    def fake_get_registration_task(db, task_uuid):
        class FakeTask:
            def __init__(self, status):
                self.status = status
                self.error_message = ""

        return FakeTask(statuses[task_uuid])

    def fake_update_registration_task(db, task_uuid, **kwargs):
        statuses[task_uuid] = kwargs.get("status", statuses[task_uuid])
        return None

    monkeypatch.setattr(registration, "get_db", fake_get_db)
    monkeypatch.setattr(registration, "run_registration_task", fake_run_registration_task)
    monkeypatch.setattr(registration.crud, "get_registration_task", fake_get_registration_task)
    monkeypatch.setattr(registration.crud, "update_registration_task", fake_update_registration_task)

    async def scenario():
        worker = asyncio.create_task(
            registration.run_batch_parallel(
                batch_id=batch_id,
                task_uuids=task_uuids,
                email_service_type="tempmail",
                proxy=None,
                email_service_config=None,
                email_service_id=None,
                concurrency=1,
            )
        )

        while not started:
            await asyncio.sleep(0.05)

        await registration.cancel_batch(batch_id)
        release_first.set()
        await asyncio.wait_for(worker, timeout=2)

    asyncio.run(scenario())

    batch = registration.batch_tasks[batch_id]
    assert batch["completed"] == 3
    assert batch["success"] == 0
    assert batch["failed"] == 0
    assert batch["finished"] is True

    for task_uuid in task_uuids:
        task_manager.cleanup_task(task_uuid)


def test_pipeline_cancelled_remaining_tasks_contribute_to_completed_progress(monkeypatch):
    batch_id = "pipeline-progress-cancel-batch"
    task_uuids = ["pipeline-progress-1", "pipeline-progress-2", "pipeline-progress-3"]
    started = []
    statuses = {task_uuid: "pending" for task_uuid in task_uuids}

    @contextmanager
    def fake_get_db():
        class FakeDb:
            pass

        yield FakeDb()

    async def fake_run_registration_task(task_uuid, *args, **kwargs):
        started.append(task_uuid)
        statuses[task_uuid] = "completed"

    def fake_get_registration_task(db, task_uuid):
        class FakeTask:
            def __init__(self, status):
                self.status = status
                self.error_message = ""

        return FakeTask(statuses[task_uuid])

    def fake_update_registration_task(db, task_uuid, **kwargs):
        statuses[task_uuid] = kwargs.get("status", statuses[task_uuid])
        return None

    monkeypatch.setattr(registration, "get_db", fake_get_db)
    monkeypatch.setattr(registration, "run_registration_task", fake_run_registration_task)
    monkeypatch.setattr(registration.crud, "get_registration_task", fake_get_registration_task)
    monkeypatch.setattr(registration.crud, "update_registration_task", fake_update_registration_task)

    async def scenario():
        worker = asyncio.create_task(
            registration.run_batch_pipeline(
                batch_id=batch_id,
                task_uuids=task_uuids,
                email_service_type="tempmail",
                proxy=None,
                email_service_config=None,
                email_service_id=None,
                interval_min=5,
                interval_max=5,
                concurrency=1,
            )
        )

        while not started:
            await asyncio.sleep(0.05)

        await registration.cancel_batch(batch_id)
        await asyncio.wait_for(worker, timeout=2)

    asyncio.run(scenario())

    batch = registration.batch_tasks[batch_id]
    assert batch["completed"] == 3
    assert batch["success"] == 1
    assert batch["failed"] == 0
    assert batch["finished"] is True

    for task_uuid in task_uuids:
        task_manager.cleanup_task(task_uuid)


def test_mark_batch_tasks_cancelled_is_idempotent(monkeypatch):
    batch_id = "idempotent-cancel-batch"
    task_uuids = ["idempotent-task-1", "idempotent-task-2"]
    statuses = {task_uuid: "pending" for task_uuid in task_uuids}

    @contextmanager
    def fake_get_db():
        class FakeDb:
            pass

        yield FakeDb()

    def fake_get_registration_task(db, task_uuid):
        class FakeTask:
            def __init__(self, status):
                self.status = status

        return FakeTask(statuses[task_uuid])

    def fake_update_registration_task(db, task_uuid, **kwargs):
        statuses[task_uuid] = kwargs.get("status", statuses[task_uuid])
        return None

    monkeypatch.setattr(registration, "get_db", fake_get_db)
    monkeypatch.setattr(registration.crud, "get_registration_task", fake_get_registration_task)
    monkeypatch.setattr(registration.crud, "update_registration_task", fake_update_registration_task)

    registration._init_batch_state(batch_id, task_uuids)

    registration._mark_batch_tasks_cancelled(batch_id, task_uuids)
    registration._mark_batch_tasks_cancelled(batch_id, task_uuids)

    batch = registration.batch_tasks[batch_id]
    assert batch["completed"] == 2
    assert statuses[task_uuids[0]] == "cancelled"
    assert statuses[task_uuids[1]] == "cancelled"

    for task_uuid in task_uuids:
        task_manager.cleanup_task(task_uuid)
