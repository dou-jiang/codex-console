from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import func

from src.core.account_survival import probe_claimed_account_survival
from src.database import crud
from src.database.models import Account, AccountSurvivalCheck, RegistrationTask
from src.database import session as session_module

logger = logging.getLogger(__name__)


class AccountSurvivalDispatcher:
    def __init__(
        self,
        *,
        repo: Any,
        probe_func: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        interval_seconds: int = 60,
    ) -> None:
        self.repo = repo
        self.probe_func = probe_func or probe_claimed_account_survival
        self.interval_seconds = max(1, int(interval_seconds))
        self._running = False
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._running = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        if self._task is None or self._task.done():
            self._task = loop.create_task(self._run_loop())

    def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def dispatch_due_checks_once(self) -> int:
        claimed = list(self.repo.claim_due_checks(limit=50))
        for item in claimed:
            result = self.probe_func(item)
            self.repo.record_result(item, result)
        return len(claimed)

    async def _run_loop(self) -> None:
        while self._running:
            try:
                self.dispatch_due_checks_once()
            except Exception as exc:
                logger.warning("account survival dispatch failed: %s", exc)
            await asyncio.sleep(self.interval_seconds)


class DatabaseAccountSurvivalRepository:
    def __init__(self, *, session_factory: Callable[[], Any] | None = None, due_after: timedelta | None = None) -> None:
        manager = session_module._db_manager
        self.session_factory = session_factory or (manager.SessionLocal if manager is not None else None)
        self.due_after = due_after or timedelta(hours=24)

    def claim_due_checks(self, *, limit: int = 50) -> list[dict[str, Any]]:
        if self.session_factory is None:
            return []

        session = self.session_factory()
        try:
            cutoff = datetime.utcnow() - self.due_after
            latest_checks = (
                session.query(
                    AccountSurvivalCheck.account_id.label("account_id"),
                    func.max(AccountSurvivalCheck.checked_at).label("last_checked_at"),
                )
                .group_by(AccountSurvivalCheck.account_id)
                .subquery()
            )

            accounts = (
                session.query(Account)
                .outerjoin(latest_checks, latest_checks.c.account_id == Account.id)
                .filter(
                    (latest_checks.c.last_checked_at.is_(None))
                    | (latest_checks.c.last_checked_at <= cutoff)
                )
                .order_by(Account.id.asc())
                .limit(max(1, int(limit)))
                .all()
            )

            claimed: list[dict[str, Any]] = []
            for account in accounts:
                task = (
                    session.query(RegistrationTask)
                    .filter(RegistrationTask.email_address == account.email)
                    .order_by(RegistrationTask.created_at.desc(), RegistrationTask.id.desc())
                    .first()
                )
                claimed.append(
                    {
                        "account_id": account.id,
                        "task_uuid": task.task_uuid if task else None,
                        "pipeline_key": task.pipeline_key if task else None,
                        "experiment_batch_id": task.experiment_batch_id if task else None,
                        "check_source": "dispatcher",
                        "check_stage": "scheduled",
                        "account": {
                            "id": account.id,
                            "status": account.status,
                            "access_token": account.access_token,
                            "refresh_token": account.refresh_token,
                            "session_token": account.session_token,
                        },
                    }
                )
            return claimed
        finally:
            session.close()

    def record_result(self, claimed_check: dict[str, Any], result: dict[str, Any]) -> None:
        if self.session_factory is None:
            return
        session = self.session_factory()
        try:
            crud.create_account_survival_check(
                session,
                account_id=int(claimed_check["account_id"]),
                task_uuid=claimed_check.get("task_uuid"),
                pipeline_key=claimed_check.get("pipeline_key"),
                experiment_batch_id=claimed_check.get("experiment_batch_id"),
                check_source=str(claimed_check.get("check_source") or "dispatcher"),
                check_stage=str(claimed_check.get("check_stage") or "scheduled"),
                result_level=str(result.get("result_level") or "warning"),
                signal_type=result.get("signal_type"),
                latency_ms=result.get("latency_ms"),
                detail_json=result.get("detail_json"),
            )
        finally:
            session.close()
