import sqlite3

import pytest
from sqlalchemy import text

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


def test_create_proxy_keeps_backward_compatible_positional_args(temp_db):
    db = temp_db

    proxy = crud.create_proxy(
        db,
        "老调用",
        "http",
        "7.7.7.7",
        8081,
        "legacy-user",
        "legacy-pass",
        False,
        7,
    )

    assert proxy.username == "legacy-user"
    assert proxy.password == "legacy-pass"
    assert proxy.enabled is False
    assert proxy.priority == 7
    assert proxy.country is None
    assert proxy.city is None


def test_get_proxies_keeps_backward_compatible_positional_args(temp_db):
    db = temp_db
    crud.create_proxy(db, "启用-1", "http", "8.8.8.1", 8080, enabled=True)
    crud.create_proxy(db, "启用-2", "http", "8.8.8.2", 8080, enabled=True)
    crud.create_proxy(db, "禁用-1", "http", "8.8.8.3", 8080, enabled=False)

    rows = crud.get_proxies(db, True, 0, 1)

    assert len(rows) == 1
    assert rows[0].enabled is True


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


def test_proxy_to_dict_exposes_country_and_city(temp_db):
    db = temp_db

    proxy = crud.create_proxy(
        db,
        name="美国-纽约-001",
        type="http",
        host="9.9.9.9",
        port=8080,
        country="美国",
        city="纽约",
    )

    payload = proxy.to_dict()

    assert payload["country"] == "美国"
    assert payload["city"] == "纽约"


def test_sqlite_migrate_tables_adds_proxy_country_and_city_columns(tmp_path):
    db_path = tmp_path / "legacy-proxy-schema.db"

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE proxies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(100) NOT NULL,
                type VARCHAR(20) NOT NULL DEFAULT 'http',
                host VARCHAR(255) NOT NULL,
                port INTEGER NOT NULL,
                username VARCHAR(100),
                password VARCHAR(255),
                enabled BOOLEAN,
                is_default BOOLEAN DEFAULT 0,
                priority INTEGER,
                last_used DATETIME,
                created_at DATETIME,
                updated_at DATETIME
            )
            """
        )
        conn.commit()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    manager.migrate_tables()

    with manager.engine.connect() as conn:
        columns = {
            row["name"]
            for row in conn.execute(text("PRAGMA table_info('proxies')")).mappings().all()
        }

    assert "country" in columns
    assert "city" in columns
