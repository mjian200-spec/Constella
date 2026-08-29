from __future__ import annotations

from pathlib import Path
from typing import Any
import time

import yaml

from .assets import build_asset_structure
from .cleaning import normalize_document
from .conditions import detect_and_align_conditions
from .io import load_mineru_document, save_context_outputs
from .models import DocumentGraph, PipelineRuntime
from .packages import build_context_packages
from .package_routing import route_context_packages
from .pattern_engine import load_pattern_engine
from .resource_understanding import understand_document_resources
from .routing import finalize_routes
from .scopes import resolve_constraint_scopes
from .structure import build_document_structure


def load_runtime(
    config_dir: str | Path, *, use_llm: bool = False, llm_max_batches: int | None = None,
    use_resource_llm: bool = False, use_package_router: bool = False,
    resource_max_items: int | None = None,
) -> PipelineRuntime:
    directory = Path(config_dir)
    model_path = directory / "models.yaml"
    models: dict[str, Any] = {}
    if model_path.exists():
        models = yaml.safe_load(model_path.read_text(encoding="utf-8")).get("models", {})
    pipeline = yaml.safe_load((directory / "pipeline.yaml").read_text(encoding="utf-8")) or {}
    return PipelineRuntime(
        config_dir=directory, use_llm=use_llm, llm_max_batches=llm_max_batches,
        use_resource_llm=use_resource_llm, use_package_router=use_package_router,
        resource_max_items=resource_max_items,
        resource_workers=max(1, int(pipeline.get("resource_workers", 8))),
        package_workers=max(1, int(pipeline.get("package_workers", 16))),
        resource_model_key=str(pipeline.get("resource_model_key", "vision")),
        package_router_model_key=str(pipeline.get("package_router_model_key", "small")),
        model_config=models,
    )


def run_context_builder(input_path: str, output_dir: str, runtime: PipelineRuntime) -> DocumentGraph:
    started = time.monotonic()
    runtime.output_dir = Path(output_dir)
    graph = load_mineru_document(input_path)
    patterns = load_pattern_engine(runtime.config_dir / "patterns.yaml")
    normalize_document(graph, runtime, patterns)
    build_document_structure(graph, runtime, patterns)
    build_asset_structure(graph, runtime, patterns)
    detect_and_align_conditions(graph, runtime, patterns)
    resolve_constraint_scopes(graph, runtime)
    finalize_routes(graph, runtime, patterns)
    understand_document_resources(graph, runtime)
    packages = build_context_packages(graph, runtime)
    route_context_packages(graph, packages, runtime)
    graph.metadata["run_events"] = runtime.run_events
    graph.metadata["model_calls"] = [event for event in runtime.run_events if event.get("task") == "completion"]
    graph.metadata["elapsed_seconds"] = round(time.monotonic() - started, 3)
    save_context_outputs(graph, packages, output_dir)
    return graph
