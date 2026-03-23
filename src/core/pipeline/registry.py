from __future__ import annotations

from .definitions import PipelineDefinition


PIPELINE_REGISTRY: dict[str, PipelineDefinition] = {}


def register_pipeline(definition: PipelineDefinition) -> None:
    if definition.pipeline_key in PIPELINE_REGISTRY:
        raise ValueError(f"Pipeline already registered: {definition.pipeline_key}")
    PIPELINE_REGISTRY[definition.pipeline_key] = definition


def get_pipeline(pipeline_key: str) -> PipelineDefinition | None:
    pipeline = PIPELINE_REGISTRY.get(pipeline_key)
    if pipeline is not None:
        return pipeline

    if pipeline_key == "current_pipeline":
        from .steps.current import register_current_pipeline

        return register_current_pipeline()

    return None
