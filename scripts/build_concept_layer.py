#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from constella.concept_layer.pipeline import load_runtime, run_concept_layer


def main() -> int:
    parser = argparse.ArgumentParser(description="Build an evidence-grounded Concept Layer from Constella rules.")
    parser.add_argument("--context-output-dir", default="outputs/context_builder_full_20260826")
    parser.add_argument("--rule-output-dir", default="outputs/rule_extraction_full_routed_20260826")
    parser.add_argument("--output-dir", default="outputs/concept_layer")
    parser.add_argument("--config-dir", default=str(ROOT / "configs" / "concept_layer"))
    parser.add_argument("--rule-id", action="append", dest="rule_ids")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--use-llm", action="store_true", help="Resolve concepts, definitions and direct parents with the configured evidence-constrained model.")
    parser.add_argument("--model-key", default="qwen3_8_27b")
    parser.add_argument("--workers", type=int, help="Maximum concurrent LLM seeds; defaults to pipeline config.")
    args = parser.parse_args()
    runtime = load_runtime(args.config_dir, args.output_dir, use_llm=args.use_llm, model_key=args.model_key)
    if args.workers is not None:
        runtime.max_workers = max(1, args.workers)
    report = run_concept_layer(
        args.context_output_dir,
        args.rule_output_dir,
        runtime,
        rule_ids=set(args.rule_ids or []) or None,
        limit=args.limit,
    )
    print(
        f"{report['rule_count']} rules -> {report['unique_object_seed_count']} object seeds -> "
        f"{report['concept_count']} concepts, {report['direct_is_a_count']} direct IS_A, "
        f"{report['binding_count']} bindings"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
