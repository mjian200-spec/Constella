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
    AlignmentInputs,
    SemanticAlignmentRunner,
    SemanticPackageBuilder,
    assemble_concepts,
    assemble_object_alignments,
    assemble_state_object_alignments,
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
            result["_package"] = {case["object_id"]: case for case in package["cases"]}
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
    parser.add_argument(
        "--stage", choices=("concept", "object", "refine", "state-reparse", "state", "all"), default="all",
    )
    parser.add_argument("--concept-limit", type=int)
    parser.add_argument("--object-limit", type=int)
    parser.add_argument("--state-reparse-limit", type=int)
    parser.add_argument("--state-limit", type=int)
    parser.add_argument("--refine-iterations", type=int, default=2)
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
        proposed_pair_count = sum(len(package["cases"]) for package in review_packages)
        accepted_pair_count = sum(
            len(result["output"].get("merge_pairs", []))
            for result in review_results if result.get("status") == "success"
        )
        concepts, relations, id_map, assembly_report = assemble_concepts(inputs, review_results)
        write_jsonl(output_dir / "concepts_merged.jsonl", concepts)
        write_jsonl(output_dir / "concept_relations_merged.jsonl", relations)
        write_json(output_dir / "concept_id_map.json", id_map)
        reports["concept"] = {
            "proposal": proposal_report,
            "review": review_report,
            "proposed_pair_count": proposed_pair_count,
            "accepted_pair_count": accepted_pair_count,
            "proposal_acceptance_rate": round(accepted_pair_count / proposed_pair_count, 4)
            if proposed_pair_count else 1.0,
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
    elif args.stage in {"refine", "state-reparse", "state"}:
        refined_concepts = output_dir / "concepts_refined.jsonl"
        concepts = _read_jsonl(refined_concepts if refined_concepts.is_file() else output_dir / "concepts_aligned.jsonl")

    if args.stage in {"refine", "all"}:
        if args.stage == "refine":
            refined_alignments = output_dir / "object_alignments_refined.jsonl"
            alignments = _read_jsonl(
                refined_alignments if refined_alignments.is_file() else output_dir / "object_alignments.jsonl"
            )
        object_metadata = {item["object_id"]: item for item in builder.object_rows.values()}
        for row in alignments:
            metadata = object_metadata.get(row.get("object_id"), {})
            if not int(row.get("frequency") or 0):
                row["frequency"] = int(metadata.get("frequency") or 0)
            if not row.get("object_name"):
                row["object_name"] = metadata.get("name")
            if not row.get("state_examples"):
                row["state_examples"] = list(metadata.get("states") or [])
        if args.stage == "refine":
            refined_relations = output_dir / "concept_relations_refined.jsonl"
            relations = _read_jsonl(
                refined_relations if refined_relations.is_file() else output_dir / "concept_relations_merged.jsonl"
            )
        prior_iterations = reports.get("refine", {}).get("iterations", []) if args.stage == "refine" else []
        iteration_reports: list[dict[str, Any]] = list(prior_iterations) if isinstance(prior_iterations, list) else []
        first_iteration_number = len(iteration_reports) + 1
        for offset in range(args.refine_iterations):
            iteration = first_iteration_number + offset
            expanded_inputs = AlignmentInputs(
                concepts=concepts,
                relations=relations,
                rules=inputs.rules,
                context_packages=inputs.context_packages,
                units=inputs.units,
            )
            expanded_builder = SemanticPackageBuilder(expanded_inputs)
            new_ids = {
                str(row["concept_id"]) for row in concepts
                if row.get("audit_status") == "alignment_created" and not row.get("source_concept_ids")
            }
            fusion_report: dict[str, Any] = {
                "new_concept_anchor_count": len(new_ids),
                "proposal_package_count": 0,
                "review_package_count": 0,
            }
            if new_ids:
                expansion_packages = expanded_builder.concept_merge_packages(anchor_ids=new_ids)
                proposal_results, proposal_report = runner.run(expansion_packages, refresh=args.refresh)
                review_packages = expanded_builder.concept_merge_review_packages(proposal_results)
                review_results, review_report = runner.run(review_packages, refresh=args.refresh)
                proposed_pair_count = sum(len(package["cases"]) for package in review_packages)
                accepted_pair_count = sum(
                    len(result["output"].get("merge_pairs", []))
                    for result in review_results if result.get("status") == "success"
                )
                concepts, relations, id_map, assembly_report = assemble_concepts(expanded_inputs, review_results)
                for row in alignments:
                    concept_id = row.get("concept_id")
                    if concept_id in id_map:
                        row["concept_id"] = id_map[concept_id]
                fusion_report = {
                    "new_concept_anchor_count": len(new_ids),
                    "proposal": proposal_report,
                    "review": review_report,
                    "proposed_pair_count": proposed_pair_count,
                    "accepted_pair_count": accepted_pair_count,
                    "proposal_acceptance_rate": round(accepted_pair_count / proposed_pair_count, 4)
                    if proposed_pair_count else 1.0,
                    **assembly_report,
                }

            reparse_ids = {row["object_id"] for row in alignments if row.get("decision") == "REPARSE"}
            before_weight = sum(int(row.get("frequency") or 0) for row in alignments if row.get("decision") == "REPARSE")
            current_inputs = AlignmentInputs(
                concepts=concepts,
                relations=relations,
                rules=inputs.rules,
                context_packages=inputs.context_packages,
                units=inputs.units,
            )
            current_builder = SemanticPackageBuilder(current_inputs)
            retry_packages = current_builder.object_alignment_packages(concepts, object_ids=reparse_ids)
            retry_results, retry_run_report = runner.run(retry_packages, refresh=args.refresh)
            _enrich_results(retry_results, retry_packages)
            retries, concepts, retry_assembly_report = assemble_object_alignments(retry_results, concepts)
            retry_by_id = {row["object_id"]: row for row in retries}
            alignments = [retry_by_id.get(row["object_id"], row) for row in alignments]
            after_reparse = [row for row in alignments if row.get("decision") == "REPARSE"]
            after_weight = sum(int(row.get("frequency") or 0) for row in after_reparse)
            iteration_report = {
                "iteration": iteration,
                "fusion": fusion_report,
                "reparse_before_count": len(reparse_ids),
                "reparse_after_count": len(after_reparse),
                "reparse_before_weight": before_weight,
                "reparse_after_weight": after_weight,
                "object_retry": {**retry_run_report, **retry_assembly_report},
            }
            iteration_reports.append(iteration_report)
            write_jsonl(output_dir / "concepts_refined.jsonl", concepts)
            write_jsonl(output_dir / "concept_relations_refined.jsonl", relations)
            write_jsonl(output_dir / "object_alignments_refined.jsonl", alignments)
            write_json(output_dir / f"refine_iteration_{iteration}.json", iteration_report)
            if len(after_reparse) >= len(reparse_ids) and not any(
                row.get("decision") == "NEW" for row in retries
            ):
                break
        reports["refine"] = {"iterations": iteration_reports}

    if args.stage in {"state-reparse", "all"}:
        if args.stage == "state-reparse":
            refined_alignments = output_dir / "object_alignments_refined.jsonl"
            alignments = _read_jsonl(
                refined_alignments if refined_alignments.is_file() else output_dir / "object_alignments.jsonl"
            )
        reparse_ids = {row["object_id"] for row in alignments if row.get("decision") == "REPARSE"}
        state_object_packages = builder.state_object_alignment_packages(reparse_ids, concepts)
        results, run_report = runner.run(
            state_object_packages, limit=args.state_reparse_limit, refresh=args.refresh,
        )
        by_package = {package["package_id"]: package for package in state_object_packages}
        for result in results:
            package = by_package[result["package_id"]]
            result["_package"] = {case["state_id"]: case for case in package["cases"]}
        state_alignments, assembly_report = assemble_state_object_alignments(results)
        write_jsonl(output_dir / "state_object_alignments.jsonl", state_alignments)
        reports["state_reparse"] = {
            **run_report,
            **assembly_report,
            "source_reparse_object_count": len(reparse_ids),
            "generated_package_count": len(state_object_packages),
        }

    if args.stage in {"state", "all"}:
        if args.stage == "state":
            refined_alignments = output_dir / "object_alignments_refined.jsonl"
            alignments = _read_jsonl(
                refined_alignments if refined_alignments.is_file() else output_dir / "object_alignments.jsonl"
            )
        alignment_map = {row["object_id"]: row["concept_id"] for row in alignments}
        state_alignment_path = output_dir / "state_object_alignments.jsonl"
        state_alignment_map = {}
        if state_alignment_path.is_file():
            state_alignment_map = {
                row["state_id"]: row["concept_id"] for row in _read_jsonl(state_alignment_path)
                if row.get("decision") == "ALIGNED"
            }
        state_packages = builder.state_normalization_packages(
            alignment_map, concepts, state_alignments=state_alignment_map,
        )
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
