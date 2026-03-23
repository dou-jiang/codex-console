import asyncio
from contextlib import contextmanager
from pathlib import Path
import sys
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
from src.web.routes import accounts as accounts_routes
from src.core.openai import token_refresh


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class FakeSession:
    def get(self, url, headers=None, timeout=None):
        token = (headers or {}).get("authorization", "").split()[-1]
        if token == "valid-token":
            return FakeResponse(200)
        return FakeResponse(500)


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


def test_batch_validate_marks_invalid_accounts_failed_and_failed_filter_returns_them(monkeypatch):
    manager = _build_manager("account_validation_filters.db")

    with manager.session_scope() as session:
        valid_account = Account(
            email="valid@example.com",
            email_service="tempmail",
            access_token="valid-token",
            status="active",
        )
        invalid_account = Account(
            email="invalid@example.com",
            email_service="tempmail",
            access_token="invalid-token",
            status="active",
        )
        session.add_all([valid_account, invalid_account])
        session.flush()
        account_ids = [valid_account.id, invalid_account.id]

    fake_get_db = _build_get_db(manager)

    monkeypatch.setattr(accounts_routes, "get_db", fake_get_db)
    monkeypatch.setattr(token_refresh, "get_db", fake_get_db)
    monkeypatch.setattr(
        token_refresh.TokenRefreshManager,
        "_create_session",
        lambda self: FakeSession(),
    )

    result = asyncio.run(
        accounts_routes.batch_validate_tokens(
            accounts_routes.BatchValidateRequest(ids=account_ids)
        )
    )

    assert result["valid_count"] == 1
    assert result["invalid_count"] == 1

    with manager.session_scope() as session:
        accounts = {
            account.email: account.status
            for account in session.query(Account).order_by(Account.email.asc()).all()
        }

    assert accounts["valid@example.com"] == "active"
    assert accounts["invalid@example.com"] == "failed"

    filtered = asyncio.run(
        accounts_routes.list_accounts(
            page=1,
            page_size=20,
            status="failed",
            email_service=None,
            search=None,
        )
    )

    assert filtered.total == 1
    assert len(filtered.accounts) == 1
    assert filtered.accounts[0].email == "invalid@example.com"
