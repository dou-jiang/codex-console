import pytest

from src.database import crud
from src.database.models import Base
from src.database.session import DatabaseSessionManager


@pytest.fixture
def temp_db(tmp_path):
    db_path = tmp_path / "proxy-settings.db"
    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    session = manager.SessionLocal()
    try:
        yield session
    finally:
        session.close()


def test_get_proxies_supports_keyword_status_default_and_location_filters(temp_db):
    db = temp_db

    first = crud.create_proxy(
        db,
        name="美国-西雅图-001",
        type="http",
        host="1.1.1.1",
        port=8080,
        country="美国",
        city="西雅图",
    )
    crud.create_proxy(
        db,
        name="代理-001",
        type="socks5",
        host="2.2.2.2",
        port=1080,
        enabled=False,
    )
    crud.set_proxy_default(db, proxy_id=first.id)

    rows = crud.get_proxies(
        db,
        keyword="美国",
        type="http",
        enabled=True,
        is_default=True,
        location="西雅图",
    )

    assert [row.host for row in rows] == ["1.1.1.1"]


def test_find_proxy_by_host_port_crud_helper_matches_exact_proxy(temp_db):
    db = temp_db

    created = crud.create_proxy(
        db,
        name="代理-精确匹配",
        type="http",
        host="5.5.5.5",
        port=3128,
    )

    matched = crud.find_proxy_by_host_port(db, host="5.5.5.5", port=3128)

    assert matched is not None
    assert matched.id == created.id


def test_delete_proxies_batch_ignores_missing_ids(temp_db):
    db = temp_db

    created = crud.create_proxy(
        db,
        name="代理-待删除",
        type="http",
        host="3.3.3.3",
        port=8080,
    )

    deleted = crud.delete_proxies(db, [created.id, 999])

    assert deleted == 1
