from contextlib import contextmanager
from datetime import datetime
import logging
from pathlib import Path
import sys
import time
import types

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


requests_module = types.ModuleType("curl_cffi.requests")


class _DummySession:
    pass


class _DummyResponse:
    pass


class _DummyRequestsError(Exception):
    pass


requests_module.Session = _DummySession
requests_module.Response = _DummyResponse
requests_module.RequestsError = _DummyRequestsError

curl_cffi_module = types.ModuleType("curl_cffi")
curl_cffi_module.requests = requests_module
curl_cffi_module.CurlMime = type("CurlMime", (), {})

sys.modules.setdefault("curl_cffi", curl_cffi_module)
sys.modules.setdefault("curl_cffi.requests", requests_module)

from src.database.models import Base, Account
from src.database.session import DatabaseSessionManager
from src.core.openai import token_refresh
from src.core.openai.token_refresh import TokenRefreshResult
from src.web.routes import accounts as accounts_routes
from src.web.routes import payment as payment_routes


def _build_manager(db_name: str) -> DatabaseSessionManager:
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / db_name
    if db_path.exists():
        db_path.unlink()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)
    return manager


def _build_get_db(manager: DatabaseSessionManager):
    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    return fake_get_db


def test_batch_check_subscription_updates_account_and_returns_result(monkeypatch):
    manager = _build_manager("account_batch_operation_routes_subscription.db")

    with manager.session_scope() as session:
        account = Account(
            email="team@example.com",
            email_service="tempmail",
            status="active",
            subscription_type=None,
        )
        session.add(account)
        session.flush()
        account_id = account.id

    fake_get_db = _build_get_db(manager)
    monkeypatch.setattr(payment_routes, "get_db", fake_get_db)
    monkeypatch.setattr(payment_routes, "check_subscription_status", lambda account, proxy: "team")

    response = payment_routes.batch_check_subscription(
        payment_routes.BatchCheckSubscriptionRequest(ids=[account_id], concurrency=2)
    )

    assert response["success_count"] == 1
    assert response["failed_count"] == 0
    assert response["details"][0]["id"] == account_id
    assert response["details"][0]["email"] == "team@example.com"
    assert response["details"][0]["success"] is True
    assert response["details"][0]["subscription_type"] == "team"

    with manager.session_scope() as session:
        updated = session.query(Account).filter(Account.id == account_id).first()
        assert updated is not None
        assert updated.subscription_type == "team"
        assert updated.subscription_at is not None
def test_batch_refresh_processes_ids_concurrently(monkeypatch):
    manager = _build_manager("account_batch_operation_routes_batch_refresh.db")
    fake_get_db = _build_get_db(manager)

    monkeypatch.setattr(accounts_routes, "get_db", fake_get_db)
    monkeypatch.setattr(accounts_routes, "_get_proxy", lambda _request_proxy=None: None)

    def slow_refresh(account_id, proxy):
        time.sleep(0.18)
        return TokenRefreshResult(
            success=True,
            access_token=f"token-{account_id}",
            expires_at=datetime.utcnow(),
        )

    monkeypatch.setattr(accounts_routes, "do_refresh", slow_refresh)

    started_at = time.perf_counter()
    result = accounts_routes.batch_refresh_tokens(
        accounts_routes.BatchRefreshRequest(ids=[1, 2], concurrency=2)
    )
    elapsed = time.perf_counter() - started_at

    assert result["success_count"] == 2
    assert result["failed_count"] == 0
    assert elapsed < 0.30


def test_batch_validate_processes_ids_concurrently(monkeypatch):
    manager = _build_manager("account_batch_operation_routes_batch_validate.db")
    fake_get_db = _build_get_db(manager)

    monkeypatch.setattr(accounts_routes, "get_db", fake_get_db)
    monkeypatch.setattr(accounts_routes, "_get_proxy", lambda _request_proxy=None: None)

    def slow_validate(account_id, proxy):
        time.sleep(0.18)
        return True, None

    monkeypatch.setattr(accounts_routes, "do_validate", slow_validate)

    started_at = time.perf_counter()
    result = accounts_routes.batch_validate_tokens(
        accounts_routes.BatchValidateRequest(ids=[1, 2], concurrency=2)
    )
    elapsed = time.perf_counter() - started_at

    assert result["valid_count"] == 2
    assert result["invalid_count"] == 0
    assert elapsed < 0.30


def test_batch_check_subscription_processes_ids_concurrently(monkeypatch):
    manager = _build_manager("account_batch_operation_routes_batch_subscription.db")

    with manager.session_scope() as session:
        first = Account(email="sub-a@example.com", email_service="tempmail", access_token="token-a", status="active")
        second = Account(email="sub-b@example.com", email_service="tempmail", access_token="token-b", status="active")
        session.add_all([first, second])
        session.flush()
        ids = [first.id, second.id]

    fake_get_db = _build_get_db(manager)
    monkeypatch.setattr(payment_routes, "get_db", fake_get_db)

    def slow_subscription_check(account, proxy):
        time.sleep(0.18)
        return "plus"

    monkeypatch.setattr(payment_routes, "check_subscription_status", slow_subscription_check)

    started_at = time.perf_counter()
    result = payment_routes.batch_check_subscription(
        payment_routes.BatchCheckSubscriptionRequest(ids=ids, concurrency=2)
    )
    elapsed = time.perf_counter() - started_at

    assert result["success_count"] == 2
    assert result["failed_count"] == 0
    assert elapsed < 0.30


def test_validate_account_token_emits_per_account_logs(caplog, monkeypatch):
    manager = _build_manager("account_batch_operation_routes_validate_logs.db")

    with manager.session_scope() as session:
        account = Account(
            email="validate-log@example.com",
            email_service="tempmail",
            access_token="invalid-token",
            status="active",
        )
        session.add(account)
        session.flush()
        account_id = account.id

    fake_get_db = _build_get_db(manager)
    monkeypatch.setattr(token_refresh, "get_db", fake_get_db)
    monkeypatch.setattr(
        token_refresh.TokenRefreshManager,
        "validate_token",
        lambda self, access_token: (False, "Token 无效或已过期"),
    )

    with caplog.at_level(logging.INFO):
        is_valid, error = token_refresh.validate_account_token(account_id)

    assert is_valid is False
    assert error == "Token 无效或已过期"
    messages = [record.getMessage() for record in caplog.records]
    assert any("开始验证账号 Token: validate-log@example.com" in message for message in messages)
    assert any(
        "账号 Token 验证失败: validate-log@example.com, 原因: Token 无效或已过期" in message
        for message in messages
    )


def test_check_subscription_emits_per_account_logs(caplog, monkeypatch):
    manager = _build_manager("account_batch_operation_routes_subscription_logs.db")

    with manager.session_scope() as session:
        account = Account(
            email="subscription-log@example.com",
            email_service="tempmail",
            access_token="valid-token",
            status="active",
        )
        session.add(account)
        session.flush()
        account_id = account.id

    fake_get_db = _build_get_db(manager)
    monkeypatch.setattr(payment_routes, "get_db", fake_get_db)
    monkeypatch.setattr(payment_routes, "check_subscription_status", lambda account, proxy: "plus")

    with caplog.at_level(logging.INFO):
        response = payment_routes.batch_check_subscription(
            payment_routes.BatchCheckSubscriptionRequest(ids=[account_id], concurrency=2)
        )

    assert response["success_count"] == 1
    messages = [record.getMessage() for record in caplog.records]
    assert any("开始检测账号订阅状态: subscription-log@example.com" in message for message in messages)
    assert any(
        "账号订阅检测成功: subscription-log@example.com, 结果: plus" in message
        for message in messages
    )
