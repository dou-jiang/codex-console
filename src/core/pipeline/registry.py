from __future__ import annotations

from .definitions import PipelineDefinition


PIPELINE_REGISTRY: dict[str, PipelineDefinition] = {}


def register_pipeline(definition: PipelineDefinition) -> None:
    PIPELINE_REGISTRY[definition.pipeline_key] = definition


def get_pipeline(pipeline_key: str) -> PipelineDefinition | None:
    return PIPELINE_REGISTRY.get(pipeline_key)
