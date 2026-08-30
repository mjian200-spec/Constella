#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from constella.semantic_alignment import (  # noqa: E402
    MemorySnapshot,
    SerialConceptAdmissionRunner,
    audit_concept_library,
    build_initial_pending_concepts,
    build_pending_concepts_from_proposals,
    load_alignment_inputs,
)
from constella.semantic_alignment.assembly import write_json, write_jsonl  # noqa: E402
from constella.semantic_alignment.evaluation import read_jsonl  # noqa: E402


def _distribution(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
    return dict(sorted(Counter(str(row.get(field) or "UNKNOWN") for row in rows).items()))


def _catalog_with_candidates(
    catalog: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id = {str(row["concept_id"]): dict(row) for row in catalog}
    for row in candidates:
        concept_id = str(row["concept_id"])
        if concept_id in by_id:
            continue
        by_id[concept_id] = {
            "concept_id": concept_id,
            "canonical_name": row["canonical_name"],
            "aliases": list(row.get("aliases") or []),
            "definition": row.get("definition"),
            "definition_type": row.get("definition_type"),
            "evidence_ids": list(row.get("evidence_ids") or []),
            "source_package_ids": list(row.get("source_package_ids") or []),
            "source_seed_ids": list(row.get("source_seed_ids") or []),
            "origin_depth": int(row.get("origin_depth") or 0),
            "registration_status": "CANDIDATE",
        }
    return sorted(by_id.values(), key=lambda row: str(row["concept_id"]))


def _write_concept_input(
    directory: Path,
    catalog: list[dict[str, Any]],
    relations: list[dict[str, Any]],
) -> None:
    write_jsonl(directory / "concepts.jsonl", catalog)
    write_jsonl(directory / "concept_relations.jsonl", relations)


def _alignment_suffix(object_limit: int | None) -> str:
    return f"_trial_limit_{object_limit}" if object_limit is not None else ""


def _run_alignment(
    args: argparse.Namespace,
    *,
    concept_dir: Path,
    reviewed_memory: Path,
    output_dir: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    command = [
        sys.executable, str(ROOT / "scripts" / "align_semantics.py"),
        "--rule-output-dir", args.rule_output_dir,
        "--concept-output-dir", str(concept_dir),
        "--context-output-dir", args.context_output_dir,
        "--output-dir", str(output_dir),
        "--reviewed-memory", str(reviewed_memory),
        "--config-dir", args.config_dir,
        "--model-key", args.model_key,
        "--max-tier", "H3",
        "--proposal-threshold", str(args.proposal_threshold),
        "--candidates-per-object", str(args.candidates_per_object),
        "--objects-per-package", str(args.objects_per_package),
        "--max-package-chars", str(args.max_package_chars),
    ]
    if args.workers is not None:
        command.extend(["--workers", str(args.workers)])
    if args.object_limit is not None:
        command.extend(["--object-limit", str(args.object_limit)])
    if args.refresh_alignments:
        command.append("--refresh")
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "process_stdout.log").write_text(completed.stdout, encoding="utf-8")
    (output_dir / "process_stderr.log").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode:
        raise RuntimeError(
            f"semantic alignment failed with {completed.returncode}; "
            f"see {output_dir / 'process_stderr.log'}"
        )
    suffix = _alignment_suffix(args.object_limit)
    report_path = output_dir / f"alignment_report{suffix}.json"
    proposals_path = output_dir / f"alignment_proposals{suffix}.jsonl"
    return json.loads(report_path.read_text(encoding="utf-8")), read_jsonl(proposals_path)


def _summary(
    *,
    cycles: list[dict[str, Any]],
    events: list[dict[str, Any]],
    memory: MemorySnapshot,
    stop_reason: str,
) -> dict[str, Any]:
    last_alignment = next(
        (row["alignment"] for row in reversed(cycles) if row.get("alignment")),
        {},
    )
    assembly = last_alignment.get("assembly") or {}
    return {
        "schema_version": "semantic_alignment.lifecycle.v1",
        "stop_reason": stop_reason,
        "cycle_count": len(cycles),
        "reviewed_memory_event_count": len(events),
        "final_memory_version": memory.version,
        "final_library_audit": audit_concept_library(memory),
        "final_object_status_counts": assembly.get("object_status_counts") or {},
        "final_state_status_counts": assembly.get("state_status_counts") or {},
        "final_invariants": assembly.get("invariants") or {},
        "cycles": cycles,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run serial concept admission and parallel object alignment until the "
            "concept/object lifecycle converges."
        ),
    )
    parser.add_argument("--output-dir", default="outputs/semantic_alignment_lifecycle")
    parser.add_argument("--rule-output-dir", default="outputs/rule_extraction_full_20260829")
    parser.add_argument("--concept-output-dir", default="outputs/article_concepts_full_20260829")
    parser.add_argument(
        "--context-output-dir", default="outputs/context_builder_semantic_qwen38_27b_20260829",
    )
    parser.add_argument("--initial-reviewed-memory")
    parser.add_argument("--config-dir", default=str(ROOT / "configs" / "concept_layer"))
    parser.add_argument("--model-key", default="qwen3_8_27b")
    parser.add_argument("--max-cycles", type=int, default=6)
    parser.add_argument("--admission-limit", type=int, help="Smoke-test only: limit each admission pass.")
    parser.add_argument("--object-limit", type=int, help="Smoke-test only: limit LLM object packages.")
    parser.add_argument("--proposal-threshold", type=int, default=1)
    parser.add_argument("--candidates-per-object", type=int, default=8)
    parser.add_argument("--objects-per-package", type=int, default=12)
    parser.add_argument("--max-package-chars", type=int, default=40_000)
    parser.add_argument("--workers", type=int)
    parser.add_argument("--refresh-admissions", action="store_true")
    parser.add_argument("--refresh-alignments", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.max_cycles < 1:
        parser.error("--max-cycles must be positive")
    if args.admission_limit is not None and args.admission_limit < 1:
        parser.error("--admission-limit must be positive")
    if args.object_limit is not None and args.object_limit < 1:
        parser.error("--object-limit must be positive")

    output = Path(args.output_dir)
    if output.exists() and any(output.iterdir()):
        parser.error("output directory must be new or empty")
    output.mkdir(parents=True, exist_ok=True)

    inputs = load_alignment_inputs(
        args.rule_output_dir, args.concept_output_dir, args.context_output_dir,
    )
    events = read_jsonl(args.initial_reviewed_memory) if args.initial_reviewed_memory else []
    catalog = [dict(row) for row in inputs.concepts]
    memory = MemorySnapshot.build(catalog, inputs.relations, events)
    pending = build_initial_pending_concepts(inputs, memory)
    initial_report = {
        "pending_concept_count": len(pending),
        "rank_confidence_counts": _distribution(pending, "rank_confidence"),
        "candidate_origin_counts": _distribution(pending, "candidate_origin"),
        "library_audit": audit_concept_library(memory),
        "input_manifest": inputs.input_manifest,
    }
    write_jsonl(output / "initial_pending_concepts.jsonl", pending)
    write_json(output / "initial_report.json", initial_report)
    if args.dry_run:
        print(json.dumps(initial_report, ensure_ascii=False, indent=2))
        return 0

    models = yaml.safe_load(
        (Path(args.config_dir) / "models.yaml").read_text(encoding="utf-8")
    )["models"]
    cycles: list[dict[str, Any]] = []
    reviewed_concept_ids: set[str] = set()
    stop_reason = "MAX_CYCLES_REACHED"
    for cycle_number in range(1, args.max_cycles + 1):
        cycle_dir = output / f"cycle_{cycle_number:03d}"
        catalog = _catalog_with_candidates(catalog, pending)
        event_count_before = len(events)
        admission = SerialConceptAdmissionRunner(
            models, args.model_key,
            ROOT / "prompts" / "semantic_alignment" / "concept_admission_v2.yaml",
            cycle_dir / "admission_cache",
        )
        reviews, events, admission_report = admission.run(
            pending,
            concepts=catalog,
            relations=inputs.relations,
            reviewed_memory=events,
            refresh=args.refresh_admissions,
            limit=args.admission_limit,
        )
        reviewed_concept_ids.update(str(row["concept_id"]) for row in reviews)
        write_jsonl(cycle_dir / "pending_concepts.jsonl", pending)
        write_jsonl(cycle_dir / "admission_reviews.jsonl", reviews)
        write_jsonl(output / "reviewed_memory.jsonl", events)

        memory = MemorySnapshot.build(catalog, inputs.relations, events)
        library_audit = audit_concept_library(memory)
        write_json(cycle_dir / "concept_library_audit.json", library_audit)
        if not all(library_audit["invariants"].values()):
            stop_reason = "CONCEPT_LIBRARY_INVARIANT_FAILED"
            cycles.append({
                "cycle": cycle_number, "admission": admission_report,
                "library_audit": library_audit, "stop_reason": stop_reason,
            })
            break

        concept_input = cycle_dir / "concept_input"
        _write_concept_input(concept_input, catalog, inputs.relations)
        alignment_report, proposals = _run_alignment(
            args,
            concept_dir=concept_input,
            reviewed_memory=output / "reviewed_memory.jsonl",
            output_dir=cycle_dir / "alignment",
        )
        pending = build_pending_concepts_from_proposals(
            proposals, inputs, memory,
            reviewed_concept_ids=reviewed_concept_ids,
        )
        write_jsonl(cycle_dir / "next_pending_concepts.jsonl", pending)
        cycle_report = {
            "cycle": cycle_number,
            "new_memory_event_count": len(events) - event_count_before,
            "admission": admission_report,
            "library_audit": library_audit,
            "alignment": alignment_report,
            "next_pending_concept_count": len(pending),
            "next_rank_confidence_counts": _distribution(pending, "rank_confidence"),
        }
        cycles.append(cycle_report)
        write_json(cycle_dir / "cycle_report.json", cycle_report)
        if not pending:
            stop_reason = "NO_PENDING_CONCEPTS"
            break
        if len(events) == event_count_before:
            stop_reason = "NO_NEW_REGISTRATIONS"
            break

    final_memory = MemorySnapshot.build(catalog, inputs.relations, events)
    final_report = _summary(
        cycles=cycles, events=events, memory=final_memory, stop_reason=stop_reason,
    )
    write_json(output / "lifecycle_report.json", final_report)
    print(json.dumps(final_report, ensure_ascii=False, indent=2))
    return 0 if stop_reason != "CONCEPT_LIBRARY_INVARIANT_FAILED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
