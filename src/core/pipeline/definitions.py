from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

from .context import PipelineContext


@dataclass(frozen=True)
class StepDefinition:
    step_key: str
    handler: Callable[[PipelineContext], dict[str, Any] | None]
    impl_key: str | None = None


@dataclass(frozen=True)
class PipelineDefinition:
    pipeline_key: str
    steps: Sequence[StepDefinition]
