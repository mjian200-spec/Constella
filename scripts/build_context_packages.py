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
    args = parser.parse_args()
    runtime = load_runtime(args.config_dir, use_llm=args.use_llm, llm_max_batches=args.llm_max_batches)
    graph = run_context_builder(args.input_path, args.output_dir, runtime)
    print(f"Built {len(graph.units)} units, {len(graph.relations)} relations and {len(graph.constraints)} constraints.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
