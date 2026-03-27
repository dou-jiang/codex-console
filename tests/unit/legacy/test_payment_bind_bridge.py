from src.web.routes.payment import (
    CreateBindCardTaskRequest,
    LocalAutoBindRequest,
    MarkUserActionRequest,
    SyncBindCardTaskRequest,
    ThirdPartyAutoBindRequest,
    ThirdPartyCardRequest,
    ThirdPartyProfileRequest,
    auto_bind_bind_card_task_local,
    auto_bind_bind_card_task_third_party,
    create_bind_card_task,
    delete_bind_card_task,
    list_bind_card_tasks,
    mark_bind_card_task_user_action,
    open_bind_card_task,
    sync_bind_card_task_subscription,
)


class _FakeService:
    def __init__(self):
        self.calls = []

    def create_task(self, request, **kwargs):
        self.calls.append(("create", request.account_id))
        return {"success": True, "task": {"id": 1, "status": "link_ready"}}

    def list_tasks(self, page, page_size, status, search, **kwargs):
        self.calls.append(("list", page, page_size, status, search))
        return {"total": 1, "tasks": [{"id": 1, "status": "link_ready"}]}

    def open_task(self, task_id, **kwargs):
        self.calls.append(("open", task_id))
        return {"success": True, "task": {"id": task_id, "status": "opened"}}

    def delete_task(self, task_id):
        self.calls.append(("delete", task_id))
        return {"success": True, "task_id": task_id}

    def sync_subscription(self, task_id, request, **kwargs):
        self.calls.append(("sync", task_id, request.proxy))
        return {"success": True, "task": {"id": task_id, "status": "completed"}}

    def mark_user_action(self, task_id, request, **kwargs):
        self.calls.append(("mark", task_id, request.timeout_seconds, request.interval_seconds))
        return {"success": True, "task": {"id": task_id, "status": "verifying"}}

    def auto_bind_third_party(self, task_id, request, **kwargs):
        self.calls.append(("third_party", task_id))
        return {"success": True, "task": {"id": task_id, "status": "paid_pending_sync"}}

    def auto_bind_local(self, task_id, request, **kwargs):
        self.calls.append(("local_auto", task_id))
        return {"success": True, "task": {"id": task_id, "status": "paid_pending_sync"}}


def test_legacy_create_bind_card_task_uses_phase2_service(monkeypatch):
    service = _FakeService()
    monkeypatch.setattr("src.web.routes.payment._create_phase2_payment_service", lambda: service)

    request = CreateBindCardTaskRequest(account_id=1, plan_type="plus")
    response = create_bind_card_task(request)

    assert response["success"] is True
    assert service.calls == [("create", 1)]


def test_legacy_list_bind_card_tasks_uses_phase2_service(monkeypatch):
    service = _FakeService()
    monkeypatch.setattr("src.web.routes.payment._create_phase2_payment_service", lambda: service)

    response = list_bind_card_tasks(page=1, page_size=20, status=None, search=None)

    assert response["total"] == 1
    assert service.calls == [("list", 1, 20, None, None)]


def test_legacy_open_bind_card_task_uses_phase2_service(monkeypatch):
    service = _FakeService()
    monkeypatch.setattr("src.web.routes.payment._create_phase2_payment_service", lambda: service)

    response = open_bind_card_task(7)

    assert response["success"] is True
    assert service.calls == [("open", 7)]


def test_legacy_delete_bind_card_task_uses_phase2_service(monkeypatch):
    service = _FakeService()
    monkeypatch.setattr("src.web.routes.payment._create_phase2_payment_service", lambda: service)

    response = delete_bind_card_task(9)

    assert response["success"] is True
    assert service.calls == [("delete", 9)]


def test_legacy_sync_bind_card_task_uses_phase2_service(monkeypatch):
    service = _FakeService()
    monkeypatch.setattr("src.web.routes.payment._create_phase2_payment_service", lambda: service)

    response = sync_bind_card_task_subscription(5, SyncBindCardTaskRequest(proxy="http://127.0.0.1:8080"))

    assert response["success"] is True
    assert service.calls == [("sync", 5, "http://127.0.0.1:8080")]


def test_legacy_mark_user_action_uses_phase2_service(monkeypatch):
    service = _FakeService()
    monkeypatch.setattr("src.web.routes.payment._create_phase2_payment_service", lambda: service)

    response = mark_bind_card_task_user_action(7, MarkUserActionRequest(proxy="http://127.0.0.1:8080", timeout_seconds=60, interval_seconds=10))

    assert response["success"] is True
    assert service.calls == [("mark", 7, 60, 10)]


def test_legacy_auto_bind_third_party_uses_phase2_service(monkeypatch):
    service = _FakeService()
    monkeypatch.setattr("src.web.routes.payment._create_phase2_payment_service", lambda: service)

    response = auto_bind_bind_card_task_third_party(
        3,
        ThirdPartyAutoBindRequest(
            card=ThirdPartyCardRequest(number="4242424242424242", exp_month="12", exp_year="30", cvc="123"),
            profile=ThirdPartyProfileRequest(name="Test", line1="Street", city="City", state="CA", postal="90001"),
        ),
    )

    assert response["success"] is True
    assert service.calls == [("third_party", 3)]


def test_legacy_auto_bind_local_uses_phase2_service(monkeypatch):
    service = _FakeService()
    monkeypatch.setattr("src.web.routes.payment._create_phase2_payment_service", lambda: service)

    response = auto_bind_bind_card_task_local(
        4,
        LocalAutoBindRequest(
            card=ThirdPartyCardRequest(number="4242424242424242", exp_month="12", exp_year="30", cvc="123"),
            profile=ThirdPartyProfileRequest(name="Test", line1="Street", city="City", state="CA", postal="90001"),
        ),
    )

    assert response["success"] is True
    assert service.calls == [("local_auto", 4)]
