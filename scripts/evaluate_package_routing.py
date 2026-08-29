#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from constella.context_builder.models import ContextPackage, DocumentGraph, SourceRef, Unit
from constella.context_builder.package_routing import route_context_packages
from constella.context_builder.pipeline import load_runtime


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate the four-way package router on 100 curated welding cases.")
    parser.add_argument("--cases", default=str(ROOT / "data/context_builder/package_route_cases_v1.yaml"))
    parser.add_argument("--config-dir", default=str(ROOT / "configs/context_builder"))
    parser.add_argument("--output-dir", default="outputs/package_route_evaluation")
    parser.add_argument("--model-key", default="small")
    parser.add_argument("--workers", type=int, default=16)
    args = parser.parse_args()
    corpus = yaml.safe_load(Path(args.cases).read_text(encoding="utf-8"))
    graph = DocumentGraph()
    packages: list[ContextPackage] = []
    expected: dict[str, str] = {}
    index = 0
    for label, texts in corpus["labels"].items():
        for text in texts:
            unit_id = f"case_unit_{index:03d}"
            package_id = f"case_{index:03d}"
            graph.units[unit_id] = Unit(unit_id, "passage", text, SourceRef(original_block_id=unit_id))
            packages.append(ContextPackage(package_id, [unit_id], attributes={"section_path": []}))
            expected[package_id] = label
            index += 1
    runtime = load_runtime(args.config_dir, use_package_router=True)
    if args.model_key not in runtime.model_config:
        parser.error(f"unknown model key: {args.model_key}")
    runtime.package_router_model_key = args.model_key
    runtime.package_workers = max(1, args.workers)
    runtime.output_dir = Path(args.output_dir)
    route_context_packages(graph, packages, runtime)
    confusion = Counter()
    mismatches = []
    for package in packages:
        actual = package.attributes["package_role"].get("label")
        wanted = expected[package.id]
        confusion[(wanted, actual)] += 1
        if actual != wanted:
            mismatches.append({
                "package_id": package.id, "expected": wanted, "actual": actual,
                "text": graph.units[package.core_unit_ids[0]].content,
            })
    report = {
        "case_count": len(packages), "correct_count": len(packages) - len(mismatches),
        "accuracy": round((len(packages) - len(mismatches)) / len(packages), 4),
        "confusion": {f"{left}->{right}": count for (left, right), count in sorted(confusion.items())},
        "mismatches": mismatches,
    }
    runtime.output_dir.mkdir(parents=True, exist_ok=True)
    (runtime.output_dir / "package_route_evaluation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(f"{report['correct_count']}/{report['case_count']} correct; accuracy={report['accuracy']:.2%}")
    return 0 if not mismatches else 1


if __name__ == "__main__":
    raise SystemExit(main())
