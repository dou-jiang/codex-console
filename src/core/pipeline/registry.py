from __future__ import annotations

from .definitions import PipelineDefinition


PIPELINE_REGISTRY: dict[str, PipelineDefinition] = {}


def register_pipeline(definition: PipelineDefinition) -> None:
    if definition.pipeline_key in PIPELINE_REGISTRY:
        raise ValueError(f"Pipeline already registered: {definition.pipeline_key}")
    PIPELINE_REGISTRY[definition.pipeline_key] = definition


def get_pipeline(pipeline_key: str) -> PipelineDefinition | None:
    return PIPELINE_REGISTRY.get(pipeline_key)
