from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .service import validate_trigger_payload


TaskType = Literal["cpa_cleanup", "cpa_refill", "account_refresh"]
TriggerType = Literal["cron", "interval"]
IntervalUnit = Literal["minutes", "hours"]
RunStatus = Literal["running", "success", "failed", "skipped"]
RunTriggerSource = Literal["scheduled", "manual"]


class ScheduledPlanCreate(BaseModel):
    name: str
    task_type: TaskType
    cpa_service_id: int
    trigger_type: TriggerType
    cron_expression: str | None = None
    interval_value: int | None = None
    interval_unit: IntervalUnit | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    config_meta: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True

    @model_validator(mode="after")
    def validate_trigger_fields(self) -> "ScheduledPlanCreate":
        try:
            validate_trigger_payload(
                self.trigger_type,
                cron_expression=self.cron_expression,
                interval_value=self.interval_value,
                interval_unit=self.interval_unit,
            )
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return self


class ScheduledPlanUpdate(BaseModel):
    name: str | None = None
    task_type: TaskType | None = None
    cpa_service_id: int | None = None
    trigger_type: TriggerType | None = None
    cron_expression: str | None = None
    interval_value: int | None = None
    interval_unit: IntervalUnit | None = None
    config: dict[str, Any] | None = None
    config_meta: dict[str, Any] | None = None
    enabled: bool | None = None


class ScheduledPlanResponse(BaseModel):
    id: int
    name: str
    task_type: TaskType
    cpa_service_id: int
    trigger_type: TriggerType
    cron_expression: str | None = None
    interval_value: int | None = None
    interval_unit: IntervalUnit | None = None
    config: dict[str, Any]
    config_meta: dict[str, Any] | None = None
    enabled: bool
    next_run_at: datetime | None = None
    last_run_started_at: datetime | None = None
    last_run_finished_at: datetime | None = None
    last_run_status: RunStatus | None = None
    last_success_at: datetime | None = None
    auto_disabled_reason: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class ScheduledPlanListResponse(BaseModel):
    items: list[ScheduledPlanResponse]
    total: int


class ScheduledRunResponse(BaseModel):
    id: int
    plan_id: int
    trigger_source: RunTriggerSource
    status: RunStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    summary: dict[str, Any] | None = None
    error_message: str | None = None
    logs: str | None = None
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)
