#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from constella.semantic_alignment import (
    SemanticAlignmentRunner,
    SemanticPackageBuilder,
    assemble_concepts,
    assemble_object_alignments,
    assemble_states,
    load_alignment_inputs,
)
from constella.semantic_alignment.assembly import write_json, write_jsonl


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"Required prior-stage output is missing: {path}")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _enrich_results(results, packages) -> None:
    by_id = {package["package_id"]: package for package in packages}
    for result in results:
        package = by_id[result["package_id"]]
        if package["package_type"] == "object_alignment":
            result["_package"] = {case["object_id"]: case["name"] for case in package["cases"]}
        elif package["package_type"] == "state_normalization":
            result["_concept_id"] = package["concept"]["id"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and run semantic alignment packages for concepts, objects, and states.")
    parser.add_argument("--rule-output-dir", default="outputs/rule_extraction_full_20260829")
    parser.add_argument("--concept-output-dir", default="outputs/article_concepts_full_20260829")
    parser.add_argument("--context-output-dir", default="outputs/context_builder_semantic_qwen38_27b_20260829")
    parser.add_argument("--output-dir", default="outputs/semantic_alignment_full_20260830")
    parser.add_argument("--config-dir", default=str(ROOT / "configs" / "concept_layer"))
    parser.add_argument("--model-key", default="qwen3_8_27b")
    parser.add_argument("--stage", choices=("concept", "object", "state", "all"), default="all")
    parser.add_argument("--concept-limit", type=int)
    parser.add_argument("--object-limit", type=int)
    parser.add_argument("--state-limit", type=int)
    parser.add_argument("--workers", type=int)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Build and count packages without calling the model.")
    args = parser.parse_args()

    inputs = load_alignment_inputs(args.rule_output_dir, args.concept_output_dir, args.context_output_dir)
    builder = SemanticPackageBuilder(inputs)
    concept_packages = builder.concept_merge_packages()
    object_packages = builder.object_alignment_packages()
    if args.dry_run:
        report = {
            "concept_count": len(inputs.concepts),
            "rule_count": len(inputs.rules),
            "unique_object_count": len(builder.object_rows),
            "concept_package_count": len(concept_packages),
            "concept_case_count": sum(len(item["cases"]) for item in concept_packages),
            "object_package_count": len(object_packages),
            "object_case_count": sum(len(item["cases"]) for item in object_packages),
            "context_unit_count": len(inputs.units),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    config = yaml.safe_load((Path(args.config_dir) / "models.yaml").read_text(encoding="utf-8"))
    models = config["models"]
    workers = args.workers or int(models[args.model_key].get("max_concurrency", 1))
    output_dir = Path(args.output_dir)
    report_path = output_dir / "alignment_report.json"
    reports: dict[str, Any] = {}
    if report_path.is_file():
        try:
            existing_report = json.loads(report_path.read_text(encoding="utf-8"))
            if isinstance(existing_report, dict):
                reports.update(existing_report)
        except (OSError, ValueError):
            pass
    runner = SemanticAlignmentRunner(
        models, args.model_key, ROOT / "prompts" / "semantic_alignment", output_dir, workers=workers,
    )
    if args.stage in {"concept", "all"}:
        proposal_results, proposal_report = runner.run(
            concept_packages, limit=args.concept_limit, refresh=args.refresh,
        )
        review_packages = builder.concept_merge_review_packages(proposal_results)
        review_results, review_report = runner.run(review_packages, refresh=args.refresh)
        concepts, relations, id_map, assembly_report = assemble_concepts(inputs, review_results)
        write_jsonl(output_dir / "concepts_merged.jsonl", concepts)
        write_jsonl(output_dir / "concept_relations_merged.jsonl", relations)
        write_json(output_dir / "concept_id_map.json", id_map)
        reports["concept"] = {
            "proposal": proposal_report,
            "review": review_report,
            **assembly_report,
        }
    else:
        concepts = _read_jsonl(output_dir / "concepts_merged.jsonl")

    if args.stage in {"object", "all"}:
        object_packages = builder.object_alignment_packages(concepts)
        results, run_report = runner.run(object_packages, limit=args.object_limit, refresh=args.refresh)
        _enrich_results(results, object_packages)
        alignments, concepts, assembly_report = assemble_object_alignments(results, concepts)
        write_jsonl(output_dir / "object_alignments.jsonl", alignments)
        write_jsonl(output_dir / "concepts_aligned.jsonl", concepts)
        reports["object"] = {**run_report, **assembly_report}
    elif args.stage == "state":
        concepts = _read_jsonl(output_dir / "concepts_aligned.jsonl")

    if args.stage in {"state", "all"}:
        alignments = _read_jsonl(output_dir / "object_alignments.jsonl")
        alignment_map = {row["object_id"]: row["concept_id"] for row in alignments}
        state_packages = builder.state_normalization_packages(alignment_map, concepts)
        results, run_report = runner.run(state_packages, limit=args.state_limit, refresh=args.refresh)
        _enrich_results(results, state_packages)
        states, assembly_report = assemble_states(results)
        write_jsonl(output_dir / "normalized_states.jsonl", states)
        reports["state"] = {**run_report, **assembly_report, "generated_package_count": len(state_packages)}

    write_json(report_path, reports)
    print(json.dumps(reports, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
