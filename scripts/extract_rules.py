#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from constella.rule_extraction.pipeline import load_runtime, run_rule_extraction


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract multimodal rules from Constella context packages.")
    parser.add_argument("--context-output-dir", default="outputs/context_builder")
    parser.add_argument("--output-dir", default="outputs/rule_extraction")
    parser.add_argument("--config-dir", default=str(ROOT / "configs" / "rule_extraction"))
    parser.add_argument("--package-id", action="append", dest="package_ids")
    parser.add_argument("--manifest", help="Stress manifest JSON from inspect_rule_extraction.py; selects its real context package IDs.")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--reset-graph", action="store_true", help="Explicitly clear Neo4j and start a new run when a prior run exists.")
    parser.add_argument("--asset-root")
    parser.add_argument("--model-key", default="large")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--max-tokens", type=int, help="Override model max_tokens, useful for a bounded test run.")
    parser.add_argument("--refresh-model-output", action="store_true", help="Discard cached model responses for selected packages before retrying.")
    parser.add_argument("--progress", action="store_true", help="Show live package and model-stage progress in the terminal.")
    parser.add_argument("--dry-run-resolve", action="store_true")
    parser.add_argument("--no-graph", action="store_true", help="Run model extraction, reflection, parsing and exports without connecting to Neo4j.")
    args = parser.parse_args()
    runtime = load_runtime(args.config_dir, args.output_dir, model_key=args.model_key, workers=args.workers,
                           asset_root=Path(args.asset_root) if args.asset_root else None, dry_run_resolve=args.dry_run_resolve,
                           no_graph=args.no_graph)
    runtime.max_tokens = args.max_tokens
    runtime.refresh_model_output = args.refresh_model_output
    runtime.show_progress = args.progress
    manifest_ids: set[str] = set()
    if args.manifest:
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        manifest_ids = {str(item["context_package_id"]) for item in manifest.get("records", [])}
    selected_ids = set(args.package_ids or []) | manifest_ids
    report = run_rule_extraction(args.context_output_dir, runtime, package_ids=selected_ids or None, limit=args.limit,
                                 resume=args.resume, retry_failed=args.retry_failed, reset_graph=args.reset_graph)
    print(f"Run {report['run_id']}: {report['success_count']} success, {report['no_rule_count']} no_rule, {report['failed_count']} failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
