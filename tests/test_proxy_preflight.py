import pytest

from src.core.pipeline.proxy_preflight import choose_available_proxy, run_proxy_preflight
from src.database import crud
from src.database.models import Base, ProxyCheckResult, ProxyCheckRun
from src.database.session import DatabaseSessionManager


@pytest.fixture
def temp_db(tmp_path):
    manager = DatabaseSessionManager(f"sqlite:///{tmp_path / 'proxy-preflight.db'}")
    Base.metadata.create_all(bind=manager.engine)
    session = manager.SessionLocal()
    try:
        yield session
    finally:
        session.close()


def test_choose_available_proxy_returns_random_available_result(monkeypatch):
    proxy_rows = [
        {"proxy_id": 1, "proxy_url": "http://a", "status": "available"},
        {"proxy_id": 2, "proxy_url": "http://b", "status": "available"},
    ]
    monkeypatch.setattr("random.choice", lambda items: items[-1])
    selected = choose_available_proxy(proxy_rows)
    assert selected["proxy_id"] == 2


def test_choose_available_proxy_raises_when_no_available_proxy():
    with pytest.raises(RuntimeError, match="no available proxy"):
        choose_available_proxy([
            {"proxy_id": 1, "proxy_url": "http://a", "status": "unavailable"},
        ])


def test_run_proxy_preflight_persists_results_and_updates_run(temp_db):
    proxy_a = crud.create_proxy(temp_db, name="proxy-a", type="http", host="127.0.0.1", port=9001)
    proxy_b = crud.create_proxy(temp_db, name="proxy-b", type="http", host="127.0.0.2", port=9002)
    proxy_a_id = proxy_a.id
    proxy_b_id = proxy_b.id

    proxies = [
        {"proxy_id": proxy_a_id, "proxy_url": proxy_a.proxy_url},
        {"proxy_id": proxy_b_id, "proxy_url": proxy_b.proxy_url},
    ]

    def fake_checker(proxy_row):
        if proxy_row["proxy_id"] == proxy_a_id:
            return {"status": "available", "latency_ms": 123, "country_code": "US", "ip_address": "1.1.1.1"}
        return {"status": "unavailable", "error_message": "timeout"}

    run, results = run_proxy_preflight(
        temp_db,
        scope_type="batch",
        scope_id="batch-1",
        proxies=proxies,
        check_single_proxy=fake_checker,
    )

    assert run.status == "completed"
    assert run.scope_type == "batch"
    assert run.scope_id == "batch-1"
    assert run.total_count == 2
    assert run.available_count == 1
    assert run.completed_at is not None

    assert len(results) == 2
    assert {item["proxy_id"] for item in results} == {proxy_a_id, proxy_b_id}

    stored_run = temp_db.query(ProxyCheckRun).filter(ProxyCheckRun.id == run.id).one()
    assert stored_run.total_count == 2
    assert stored_run.available_count == 1
    assert stored_run.status == "completed"

    stored_results = (
        temp_db.query(ProxyCheckResult)
        .filter(ProxyCheckResult.proxy_check_run_id == run.id)
        .order_by(ProxyCheckResult.proxy_id.asc())
        .all()
    )
    assert [item.status for item in stored_results] == ["available", "unavailable"]
    assert stored_results[0].latency_ms == 123
    assert stored_results[0].country_code == "US"
    assert stored_results[0].ip_address == "1.1.1.1"
    assert stored_results[1].error_message == "timeout"


def test_run_proxy_preflight_marks_failed_probe_as_unavailable(temp_db):
    proxy = crud.create_proxy(temp_db, name="proxy-c", type="http", host="127.0.0.3", port=9003)

    run, results = run_proxy_preflight(
        temp_db,
        scope_type="single_task",
        scope_id="task-1",
        proxies=[{"proxy_id": proxy.id, "proxy_url": proxy.proxy_url}],
        check_single_proxy=lambda _: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    assert run.status == "completed"
    assert run.total_count == 1
    assert run.available_count == 0
    assert results[0]["status"] == "unavailable"
    assert "boom" in (results[0]["error_message"] or "")


def test_run_proxy_preflight_marks_run_failed_when_result_persistence_breaks(temp_db, monkeypatch):
    proxy_a = crud.create_proxy(temp_db, name="proxy-d", type="http", host="127.0.0.4", port=9004)
    proxy_b = crud.create_proxy(temp_db, name="proxy-e", type="http", host="127.0.0.5", port=9005)
    proxy_a_id = proxy_a.id
    proxy_b_id = proxy_b.id
    original_create_result = crud.create_proxy_check_result
    call_count = {"value": 0}

    def flaky_create_result(*args, **kwargs):
        call_count["value"] += 1
        if call_count["value"] == 2:
            raise RuntimeError("persist broken")
        return original_create_result(*args, **kwargs)

    monkeypatch.setattr(
        "src.core.pipeline.proxy_preflight.crud.create_proxy_check_result",
        flaky_create_result,
    )

    with pytest.raises(RuntimeError, match="persist broken"):
        run_proxy_preflight(
            temp_db,
            scope_type="batch",
            scope_id="batch-persist-fail",
            proxies=[
                {"proxy_id": proxy_a_id, "proxy_url": proxy_a.proxy_url},
                {"proxy_id": proxy_b_id, "proxy_url": proxy_b.proxy_url},
            ],
            check_single_proxy=lambda _: {"status": "available"},
        )

    run = (
        temp_db.query(ProxyCheckRun)
        .filter(ProxyCheckRun.scope_type == "batch", ProxyCheckRun.scope_id == "batch-persist-fail")
        .one()
    )
    assert run.status == "failed"
    assert run.completed_at is not None
    assert run.total_count == 2
    assert run.available_count == 2


def test_run_proxy_preflight_falls_back_to_direct_failed_mark_when_finalization_errors(temp_db, monkeypatch):
    proxy = crud.create_proxy(temp_db, name="proxy-f", type="http", host="127.0.0.6", port=9006)

    monkeypatch.setattr(
        "src.core.pipeline.proxy_preflight.crud.finalize_proxy_check_run",
        lambda *_, **__: (_ for _ in ()).throw(RuntimeError("finalize broken")),
    )

    with pytest.raises(RuntimeError, match="finalize broken"):
        run_proxy_preflight(
            temp_db,
            scope_type="single_task",
            scope_id="task-finalize-fail",
            proxies=[{"proxy_id": proxy.id, "proxy_url": proxy.proxy_url}],
            check_single_proxy=lambda _: {"status": "available"},
        )

    run = (
        temp_db.query(ProxyCheckRun)
        .filter(ProxyCheckRun.scope_type == "single_task", ProxyCheckRun.scope_id == "task-finalize-fail")
        .one()
    )
    assert run.status == "failed"
    assert run.completed_at is not None
