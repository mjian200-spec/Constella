#!/usr/bin/env python3
from __future__ import annotations

import argparse
from itertools import product
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from constella.semantic_alignment import SemanticPackageBuilder, load_alignment_inputs  # noqa: E402
from constella.semantic_alignment.evaluation import (  # noqa: E402
    candidate_recall_metrics,
    load_weak_object_labels,
    package_metrics,
)


def _integers(value: str) -> list[int]:
    values = sorted({int(item.strip()) for item in value.split(",") if item.strip()})
    if not values or any(item < 1 for item in values):
        raise argparse.ArgumentTypeError("expected comma-separated positive integers")
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description="Grid-search package retrieval and batching parameters.")
    parser.add_argument("--rule-output-dir", default="outputs/rule_extraction_full_20260829")
    parser.add_argument("--concept-output-dir", default="outputs/article_concepts_full_20260829")
    parser.add_argument("--context-output-dir", default="outputs/context_builder_semantic_qwen38_27b_20260829")
    parser.add_argument(
        "--weak-labels", default="outputs/semantic_alignment_full_20260830/final_object_alignments.jsonl",
    )
    parser.add_argument("--candidate-grid", type=_integers, default=_integers("4,6,8"))
    parser.add_argument("--objects-grid", type=_integers, default=_integers("8,12,16"))
    parser.add_argument("--chars-grid", type=_integers, default=_integers("25000,40000"))
    parser.add_argument("--recall-tolerance", type=float, default=0.002)
    parser.add_argument("--output")
    args = parser.parse_args()

    inputs = load_alignment_inputs(args.rule_output_dir, args.concept_output_dir, args.context_output_dir)
    builder = SemanticPackageBuilder(inputs)
    weak_labels = load_weak_object_labels(args.weak_labels, builder)
    rows = []
    for candidate_count, object_count, char_limit in product(
        args.candidate_grid, args.objects_grid, args.chars_grid,
    ):
        packages = builder.object_alignment_packages(
            candidates_per_object=candidate_count,
            objects_per_package=object_count,
            max_package_chars=char_limit,
        )
        rows.append({
            "parameters": {
                "candidates_per_object": candidate_count,
                "objects_per_package": object_count,
                "max_package_chars": char_limit,
            },
            "retrieval": candidate_recall_metrics(builder, packages, weak_labels),
            "package": package_metrics(packages),
        })
    best_recall = max((row["retrieval"]["weighted_candidate_recall"] for row in rows), default=0.0)
    admissible = [
        row for row in rows
        if row["retrieval"]["weighted_candidate_recall"] >= best_recall - args.recall_tolerance
    ]
    recommended = min(
        admissible,
        key=lambda row: (
            row["package"]["package_count"], row["package"]["input_chars_total"],
            row["package"]["input_chars_p95"], row["parameters"]["candidates_per_object"],
        ),
    ) if admissible else None
    report = {
        "schema_version": "semantic_alignment.experiment.v1",
        "label_policy": "Previous approved-looking alignments are weak labels, not semantic gold.",
        "selection_policy": {
            "primary": "weighted_candidate_recall",
            "recall_tolerance": args.recall_tolerance,
            "tie_breakers": ["package_count", "input_chars_total", "input_chars_p95"],
        },
        "weak_label_count": len(weak_labels),
        "recommended": recommended,
        "experiments": rows,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
