from src.web.routes.payment import (
    CreateBindCardTaskRequest,
    create_bind_card_task,
    delete_bind_card_task,
    list_bind_card_tasks,
    open_bind_card_task,
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
