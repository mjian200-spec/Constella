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
from .pattern_engine import load_pattern_engine
from .routing import finalize_routes
from .scopes import resolve_constraint_scopes
from .structure import build_document_structure


def load_runtime(config_dir: str | Path, *, use_llm: bool = False) -> PipelineRuntime:
    directory = Path(config_dir)
    model_path = directory / "models.yaml"
    models: dict[str, Any] = {}
    if model_path.exists():
        models = yaml.safe_load(model_path.read_text(encoding="utf-8")).get("models", {})
    return PipelineRuntime(config_dir=directory, use_llm=use_llm, model_config=models)


def run_context_builder(input_path: str, output_dir: str, runtime: PipelineRuntime) -> DocumentGraph:
    started = time.monotonic()
    graph = load_mineru_document(input_path)
    patterns = load_pattern_engine(runtime.config_dir / "patterns.yaml")
    normalize_document(graph, runtime, patterns)
    build_document_structure(graph, runtime, patterns)
    build_asset_structure(graph, runtime, patterns)
    detect_and_align_conditions(graph, runtime, patterns)
    resolve_constraint_scopes(graph, runtime)
    finalize_routes(graph, runtime, patterns)
    packages = build_context_packages(graph, runtime)
    graph.metadata["run_events"] = runtime.run_events
    graph.metadata["model_calls"] = [event for event in runtime.run_events if event.get("task") == "completion"]
    graph.metadata["elapsed_seconds"] = round(time.monotonic() - started, 3)
    save_context_outputs(graph, packages, output_dir)
    return graph
