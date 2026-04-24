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
