from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PipelineContext:
    task_uuid: str
    pipeline_key: str
    experiment_batch_id: int | None = None
    pair_key: str | None = None
    proxy_url: str | None = None
    email: str | None = None
    password: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
