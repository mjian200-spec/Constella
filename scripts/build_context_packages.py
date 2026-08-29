#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from constella.context_builder.pipeline import load_runtime, run_context_builder


def main() -> int:
    parser = argparse.ArgumentParser(description="Build traceable context packages from MinerU content-list JSON.")
    parser.add_argument("input_path")
    parser.add_argument("--output-dir", default="outputs/context_builder")
    parser.add_argument("--config-dir", default=str(ROOT / "configs" / "context_builder"))
    parser.add_argument("--use-llm", action="store_true", help="Classify only low-confidence route candidates through vLLM.")
    parser.add_argument("--llm-max-batches", type=int, help="Limit LLM batches for a real-input trial; omit to classify every candidate.")
    parser.add_argument("--use-resource-llm", action="store_true", help="Use the configured VLM/LLM to textualize figures, tables, and formula symbols.")
    parser.add_argument("--use-package-router", action="store_true", help="Classify every completed context package as concept, rule, both, or noise.")
    parser.add_argument("--resource-max-items", type=int, help="Limit resource-model items for a real-input trial.")
    parser.add_argument("--resource-model-key", help="Model key for figure/table textualization and formula symbol resolution.")
    parser.add_argument("--package-router-model-key", help="Model key for final four-way package routing.")
    parser.add_argument("--resource-workers", type=int, help="Concurrent resource understanding requests.")
    parser.add_argument("--package-workers", type=int, help="Concurrent final package routing requests.")
    args = parser.parse_args()
    runtime = load_runtime(
        args.config_dir, use_llm=args.use_llm, llm_max_batches=args.llm_max_batches,
        use_resource_llm=args.use_resource_llm, use_package_router=args.use_package_router,
        resource_max_items=args.resource_max_items,
    )
    for value, field in (
        (args.resource_model_key, "resource_model_key"),
        (args.package_router_model_key, "package_router_model_key"),
    ):
        if value is not None:
            if value not in runtime.model_config:
                parser.error(f"unknown model key for --{field.replace('_', '-')}: {value}")
            setattr(runtime, field, value)
    if args.resource_workers is not None:
        runtime.resource_workers = max(1, args.resource_workers)
    if args.package_workers is not None:
        runtime.package_workers = max(1, args.package_workers)
    graph = run_context_builder(args.input_path, args.output_dir, runtime)
    print(f"Built {len(graph.units)} units, {len(graph.relations)} relations and {len(graph.constraints)} constraints.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
