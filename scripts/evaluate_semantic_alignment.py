#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from constella.semantic_alignment import MemorySnapshot, SemanticPackageBuilder, load_alignment_inputs  # noqa: E402
from constella.semantic_alignment.evaluation import (  # noqa: E402
    artifact_metrics,
    candidate_recall_metrics,
    gold_metrics,
    load_weak_object_labels,
    package_metrics,
    read_jsonl,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate semantic-alignment artifacts and package retrieval.")
    parser.add_argument("--rule-output-dir", default="outputs/rule_extraction_full_20260829")
    parser.add_argument("--concept-output-dir", default="outputs/article_concepts_full_20260829")
    parser.add_argument("--context-output-dir", default="outputs/context_builder_semantic_qwen38_27b_20260829")
    parser.add_argument("--reviewed-memory")
    parser.add_argument("--artifact-dir")
    parser.add_argument("--artifact-suffix", default="")
    parser.add_argument("--gold", help="Human-reviewed JSONL; see the v2 design document for its schema.")
    parser.add_argument("--weak-labels", help="Previous alignment JSONL, used only for candidate recall.")
    parser.add_argument("--candidates-per-object", type=int, default=6)
    parser.add_argument("--objects-per-package", type=int, default=12)
    parser.add_argument("--max-package-chars", type=int, default=40_000)
    parser.add_argument("--output")
    args = parser.parse_args()

    inputs = load_alignment_inputs(args.rule_output_dir, args.concept_output_dir, args.context_output_dir)
    reviewed = read_jsonl(args.reviewed_memory) if args.reviewed_memory else []
    builder = SemanticPackageBuilder(
        inputs, memory=MemorySnapshot.build(inputs.concepts, inputs.relations, reviewed),
    )
    packages = builder.object_alignment_packages(
        candidates_per_object=args.candidates_per_object,
        objects_per_package=args.objects_per_package,
        max_package_chars=args.max_package_chars,
    )
    report: dict[str, object] = {
        "schema_version": "semantic_alignment.evaluation.v1",
        "package": package_metrics(packages),
    }
    if args.weak_labels:
        weak_labels = load_weak_object_labels(args.weak_labels, builder)
        report["candidate_retrieval"] = candidate_recall_metrics(builder, packages, weak_labels)

    object_rows: list[dict] = []
    state_rows: list[dict] = []
    if args.artifact_dir:
        artifact_dir = Path(args.artifact_dir)
        suffix = args.artifact_suffix
        object_rows = read_jsonl(artifact_dir / f"object_semantics{suffix}.jsonl")
        state_rows = read_jsonl(artifact_dir / f"state_semantics{suffix}.jsonl")
        proposals = read_jsonl(artifact_dir / f"alignment_proposals{suffix}.jsonl")
        coverage = read_jsonl(artifact_dir / f"state_coverage{suffix}.jsonl")
        report["artifact"] = artifact_metrics(object_rows, state_rows, proposals, coverage)
    if args.gold:
        if not args.artifact_dir:
            parser.error("--gold requires --artifact-dir")
        report["gold"] = gold_metrics(object_rows, state_rows, read_jsonl(args.gold))

    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
