from __future__ import annotations

from datetime import datetime
from sqlalchemy.orm import Session

from src.database import crud
from src.database.models import PipelineStepRun

from .context import PipelineContext
from .definitions import PipelineDefinition


class PipelineRunner:
    def __init__(self, db: Session):
        self.db = db

    def run(self, pipeline: PipelineDefinition, ctx: PipelineContext) -> PipelineContext:
        pipeline_started_at = datetime.utcnow()
        ctx.pipeline_key = pipeline.pipeline_key
        crud.update_registration_task(
            self.db,
            ctx.task_uuid,
            pipeline_key=pipeline.pipeline_key,
            pipeline_status="running",
            started_at=pipeline_started_at,
        )

        for order, step in enumerate(pipeline.steps, start=1):
            started_at = datetime.utcnow()
            crud.update_registration_task(
                self.db,
                ctx.task_uuid,
                current_step_key=step.step_key,
                pipeline_status="running",
            )
            step_run = crud.create_pipeline_step_run(
                self.db,
                task_uuid=ctx.task_uuid,
                pipeline_key=pipeline.pipeline_key,
                step_key=step.step_key,
                step_order=order,
                step_impl=step.impl_key,
                status="running",
                started_at=started_at,
            )

            try:
                payload = step.handler(ctx) or {}
            except Exception as exc:
                self._finalize_step(step_run, started_at, status="failed", error_message=str(exc))
                crud.update_registration_task(
                    self.db,
                    ctx.task_uuid,
                    pipeline_status="failed",
                    total_duration_ms=self._duration_ms(pipeline_started_at, datetime.utcnow()),
                    completed_at=datetime.utcnow(),
                    error_message=str(exc),
                )
                raise

            for key, value in payload.items():
                setattr(ctx, key, value)

            self._finalize_step(step_run, started_at, status="completed")

        completed_at = datetime.utcnow()
        crud.update_registration_task(
            self.db,
            ctx.task_uuid,
            pipeline_status="completed",
            total_duration_ms=self._duration_ms(pipeline_started_at, completed_at),
            completed_at=completed_at,
        )
        return ctx

    def _finalize_step(
        self,
        step_run: PipelineStepRun,
        started_at: datetime,
        *,
        status: str,
        error_message: str | None = None,
    ) -> None:
        completed_at = datetime.utcnow()
        step_run.status = status
        step_run.completed_at = completed_at
        step_run.duration_ms = self._duration_ms(started_at, completed_at)
        step_run.error_message = error_message
        self.db.commit()
        self.db.refresh(step_run)

    @staticmethod
    def _duration_ms(started_at: datetime, completed_at: datetime) -> int:
        return max(0, int((completed_at - started_at).total_seconds() * 1000))
