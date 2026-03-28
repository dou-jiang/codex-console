"""
数据库会话管理
"""

from contextlib import contextmanager
from typing import Generator
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.exc import SQLAlchemyError
import os
import logging

from .models import Base

logger = logging.getLogger(__name__)


def _build_sqlalchemy_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + database_url[len("postgresql://"):]
    if database_url.startswith("postgres://"):
        return "postgresql+psycopg://" + database_url[len("postgres://"):]
    return database_url


class DatabaseSessionManager:
    """数据库会话管理器"""

    def __init__(self, database_url: str = None):
        if database_url is None:
            env_url = os.environ.get("APP_DATABASE_URL") or os.environ.get("DATABASE_URL")
            if env_url:
                database_url = env_url
            else:
                # 优先使用 APP_DATA_DIR 环境变量（PyInstaller 打包后由 webui.py 设置）
                data_dir = os.environ.get('APP_DATA_DIR') or os.path.join(
                    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                    'data'
                )
                db_path = os.path.join(data_dir, 'database.db')
                # 确保目录存在
                os.makedirs(data_dir, exist_ok=True)
                database_url = f"sqlite:///{db_path}"

        self.database_url = _build_sqlalchemy_url(database_url)
        self.engine = create_engine(
            self.database_url,
            connect_args={"check_same_thread": False} if self.database_url.startswith("sqlite") else {},
            echo=False,  # 设置为 True 可以查看所有 SQL 语句
            pool_pre_ping=True  # 连接池预检查
        )
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

    def get_db(self) -> Generator[Session, None, None]:
        """
        获取数据库会话的上下文管理器
        使用示例:
            with get_db() as db:
                # 使用 db 进行数据库操作
                pass
        """
        db = self.SessionLocal()
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def session_scope(self) -> Generator[Session, None, None]:
        """
        事务作用域上下文管理器
        使用示例:
            with session_scope() as session:
                # 数据库操作
                pass
        """
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()

    def create_tables(self):
        """创建所有表"""
        Base.metadata.create_all(bind=self.engine)

    def drop_tables(self):
        """删除所有表（谨慎使用）"""
        Base.metadata.drop_all(bind=self.engine)

    def _migrate_bind_card_tasks_keep_history_on_account_delete(self, conn):
        """
        迁移 bind_card_tasks：
        - account_id 允许为空（账号删除后保留任务历史）
        - 增加 account_email 快照字段用于历史展示
        """
        try:
            table_exists = conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name='bind_card_tasks'")
            ).fetchone()
            if not table_exists:
                return

            table_info = conn.execute(text("PRAGMA table_info('bind_card_tasks')")).fetchall()
            if not table_info:
                return

            column_map = {str(row[1]): row for row in table_info}
            account_info = column_map.get("account_id")
            has_account_email = "account_email" in column_map
            account_notnull = int(account_info[3]) if account_info else 0

            # 已是目标结构：仅做一次邮箱快照补齐
            if has_account_email and account_notnull == 0:
                conn.execute(text(
                    """
                    UPDATE bind_card_tasks
                    SET account_email = COALESCE(
                        NULLIF(account_email, ''),
                        (SELECT email FROM accounts WHERE accounts.id = bind_card_tasks.account_id)
                    )
                    WHERE account_email IS NULL OR TRIM(account_email) = ''
                    """
                ))
                conn.commit()
                return

            logger.info("迁移 bind_card_tasks：启用账号删除后保留任务历史")

            select_email_expr = (
                "COALESCE(NULLIF(t.account_email, ''), a.email)"
                if has_account_email
                else "a.email"
            )

            conn.execute(text("PRAGMA foreign_keys=OFF"))
            conn.commit()

            conn.execute(text(
                """
                CREATE TABLE IF NOT EXISTS bind_card_tasks_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER,
                    account_email VARCHAR(255),
                    plan_type VARCHAR(20) NOT NULL,
                    workspace_name VARCHAR(255),
                    price_interval VARCHAR(20),
                    seat_quantity INTEGER,
                    country VARCHAR(10) DEFAULT 'US',
                    currency VARCHAR(10) DEFAULT 'USD',
                    checkout_url TEXT NOT NULL,
                    checkout_session_id VARCHAR(120),
                    publishable_key VARCHAR(255),
                    client_secret TEXT,
                    checkout_source VARCHAR(50),
                    bind_mode VARCHAR(30) DEFAULT 'semi_auto',
                    status VARCHAR(20) DEFAULT 'link_ready',
                    last_error TEXT,
                    opened_at DATETIME,
                    last_checked_at DATETIME,
                    completed_at DATETIME,
                    created_at DATETIME,
                    updated_at DATETIME,
                    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE SET NULL
                )
                """
            ))

            conn.execute(text(f"""
                INSERT INTO bind_card_tasks_new (
                    id, account_id, account_email, plan_type, workspace_name, price_interval,
                    seat_quantity, country, currency, checkout_url, checkout_session_id,
                    publishable_key, client_secret, checkout_source, bind_mode, status,
                    last_error, opened_at, last_checked_at, completed_at, created_at, updated_at
                )
                SELECT
                    t.id, t.account_id, {select_email_expr}, t.plan_type, t.workspace_name, t.price_interval,
                    t.seat_quantity, t.country, t.currency, t.checkout_url, t.checkout_session_id,
                    t.publishable_key, t.client_secret, t.checkout_source, t.bind_mode, t.status,
                    t.last_error, t.opened_at, t.last_checked_at, t.completed_at, t.created_at, t.updated_at
                FROM bind_card_tasks t
                LEFT JOIN accounts a ON a.id = t.account_id
            """))

            conn.execute(text("DROP TABLE bind_card_tasks"))
            conn.execute(text("ALTER TABLE bind_card_tasks_new RENAME TO bind_card_tasks"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_bind_card_tasks_account_id ON bind_card_tasks (account_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_bind_card_tasks_status ON bind_card_tasks (status)"))
            conn.execute(text("PRAGMA foreign_keys=ON"))
            conn.commit()
        except Exception as e:
            try:
                conn.execute(text("PRAGMA foreign_keys=ON"))
                conn.commit()
            except Exception:
                pass
            logger.warning(f"迁移 bind_card_tasks 历史保留结构时出错: {e}")

    def migrate_tables(self):
        """
        数据库迁移 - 添加缺失的列
        用于在不删除数据的情况下更新表结构
        """
        if not self.database_url.startswith("sqlite"):
            logger.info("非 SQLite 数据库，跳过自动迁移")
            return

        # 需要检查和添加的新列
        migrations = [
            # (表名, 列名, 列类型)
            ("accounts", "cpa_uploaded", "BOOLEAN DEFAULT 0"),
            ("accounts", "cpa_uploaded_at", "DATETIME"),
            ("accounts", "source", "VARCHAR(20) DEFAULT 'register'"),
            ("accounts", "account_label", "VARCHAR(20) DEFAULT 'none'"),
            ("accounts", "role_tag", "VARCHAR(20) DEFAULT 'none'"),
            ("accounts", "biz_tag", "VARCHAR(80)"),
            ("accounts", "pool_state", "VARCHAR(30) DEFAULT 'candidate_pool'"),
            ("accounts", "pool_state_manual", "VARCHAR(30)"),
            ("accounts", "last_pool_sync_at", "DATETIME"),
            ("accounts", "priority", "INTEGER DEFAULT 50"),
            ("accounts", "last_used_at", "DATETIME"),
            ("accounts", "subscription_type", "VARCHAR(20)"),
            ("accounts", "subscription_at", "DATETIME"),
            ("accounts", "cookies", "TEXT"),
            ("cpa_services", "proxy_url", "VARCHAR(1000)"),
            ("sub2api_services", "target_type", "VARCHAR(50) DEFAULT 'sub2api'"),
            ("proxies", "is_default", "BOOLEAN DEFAULT 0"),
            ("bind_card_tasks", "checkout_session_id", "VARCHAR(120)"),
            ("bind_card_tasks", "publishable_key", "VARCHAR(255)"),
            ("bind_card_tasks", "client_secret", "TEXT"),
            ("bind_card_tasks", "bind_mode", "VARCHAR(30) DEFAULT 'semi_auto'"),
            ("bind_card_tasks", "account_email", "VARCHAR(255)"),
        ]

        # 确保新表存在（create_tables 已处理，此处兜底）
        Base.metadata.create_all(bind=self.engine)

        with self.engine.connect() as conn:
            # 数据迁移：将旧的 custom_domain 记录统一为 moe_mail
            try:
                conn.execute(text("UPDATE email_services SET service_type='moe_mail' WHERE service_type='custom_domain'"))
                conn.execute(text("UPDATE accounts SET email_service='moe_mail' WHERE email_service='custom_domain'"))
                conn.commit()
            except Exception as e:
                logger.warning(f"迁移 custom_domain -> moe_mail 时出错: {e}")

            for table_name, column_name, column_type in migrations:
                try:
                    # 检查列是否存在
                    result = conn.execute(text(
                        f"SELECT * FROM pragma_table_info('{table_name}') WHERE name='{column_name}'"
                    ))
                    if result.fetchone() is None:
                        # 列不存在，添加它
                        logger.info(f"添加列 {table_name}.{column_name}")
                        conn.execute(text(
                            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
                        ))
                        conn.commit()
                        logger.info(f"成功添加列 {table_name}.{column_name}")
                except Exception as e:
                    logger.warning(f"迁移列 {table_name}.{column_name} 时出错: {e}")

            # 账户标签/池状态回填与索引
            try:
                conn.execute(text(
                    """
                    UPDATE accounts
                    SET role_tag = CASE
                        WHEN LOWER(COALESCE(account_label, '')) IN ('mother', 'parent', 'manager', '母号') THEN 'parent'
                        WHEN LOWER(COALESCE(account_label, '')) IN ('child', 'member', '子号') THEN 'child'
                        ELSE 'none'
                    END
                    WHERE role_tag IS NULL OR TRIM(role_tag) = '' OR LOWER(TRIM(role_tag)) = 'none'
                    """
                ))
                conn.execute(text(
                    """
                    UPDATE accounts
                    SET account_label = CASE
                        WHEN LOWER(COALESCE(role_tag, '')) = 'parent' THEN 'mother'
                        WHEN LOWER(COALESCE(role_tag, '')) = 'child' THEN 'child'
                        ELSE 'none'
                    END
                    WHERE account_label IS NULL OR TRIM(account_label) = '' OR LOWER(TRIM(account_label)) = 'none'
                    """
                ))
                conn.execute(text(
                    "UPDATE accounts SET pool_state='candidate_pool' WHERE pool_state IS NULL OR TRIM(pool_state)=''"
                ))
                conn.execute(text(
                    "UPDATE accounts SET priority=50 WHERE priority IS NULL"
                ))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_accounts_role_tag ON accounts (role_tag)"
                ))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_accounts_biz_tag ON accounts (biz_tag)"
                ))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_accounts_pool_state ON accounts (pool_state)"
                ))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_accounts_pool_state_manual ON accounts (pool_state_manual)"
                ))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_accounts_priority ON accounts (priority)"
                ))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_accounts_last_pool_sync_at ON accounts (last_pool_sync_at)"
                ))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_accounts_last_used_at ON accounts (last_used_at)"
                ))
                conn.commit()
            except Exception as e:
                logger.warning(f"迁移账户 role_tag/pool_state 索引时出错: {e}")

            # 最后处理 bind_card_tasks 结构升级（account_id 可空 + account_email 快照）
            self._migrate_bind_card_tasks_keep_history_on_account_delete(conn)


# 全局数据库会话管理器实例
_db_manager: DatabaseSessionManager = None


def init_database(database_url: str = None) -> DatabaseSessionManager:
    """
    初始化数据库会话管理器
    """
    global _db_manager
    if _db_manager is None:
        _db_manager = DatabaseSessionManager(database_url)
        _db_manager.create_tables()
        # 执行数据库迁移
        _db_manager.migrate_tables()
    return _db_manager


def get_session_manager() -> DatabaseSessionManager:
    """
    获取数据库会话管理器
    """
    if _db_manager is None:
        raise RuntimeError("数据库未初始化，请先调用 init_database()")
    return _db_manager


@contextmanager
def get_db() -> Generator[Session, None, None]:
    """
    获取数据库会话的快捷函数
    """
    manager = get_session_manager()
    db = manager.SessionLocal()
    try:
        yield db
    finally:
        db.close()
