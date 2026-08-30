#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from constella.semantic_alignment import MemorySnapshot, load_alignment_inputs  # noqa: E402
from constella.semantic_alignment.assembly import write_json, write_jsonl  # noqa: E402
from constella.semantic_alignment.auto_promotion import (  # noqa: E402
    ConceptAdmissionGate,
    build_concept_admission_candidates,
)
from constella.semantic_alignment.evaluation import read_jsonl  # noqa: E402


def _report_summary(report: dict[str, Any]) -> dict[str, Any]:
    assembly = report.get("assembly") or {}
    evaluation = report.get("evaluation") or {}
    runner = report.get("runner") or {}
    return {
        "memory_version": report.get("memory_version"),
        "approved_memory_count": report.get("approved_memory_count"),
        "candidate_catalog_count": report.get("candidate_catalog_count"),
        "registered_concept_count": report.get("registered_concept_count"),
        "mechanical_object_count": report.get("mechanical_object_count"),
        "llm_package_count": report.get("llm_package_count"),
        "llm_case_count": report.get("llm_case_count"),
        "runner_success_count": runner.get("success_count"),
        "runner_failed_count": runner.get("failed_count"),
        "object_status_counts": assembly.get("object_status_counts") or {},
        "state_status_counts": assembly.get("state_status_counts") or {},
        "proposal_count": assembly.get("proposal_count"),
        "coverage_object_count": assembly.get("coverage_object_count"),
        "coverage_observation_count": evaluation.get("coverage_observation_count"),
        "invariants": assembly.get("invariants") or {},
    }


def _proposals_path(directory: Path) -> Path:
    exact = directory / "alignment_proposals.jsonl"
    if exact.is_file():
        return exact
    suffixed = sorted(directory.glob("alignment_proposals*.jsonl"))
    if not suffixed:
        raise FileNotFoundError(f"missing alignment_proposals*.jsonl in {directory}")
    return suffixed[0]


def _delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in (
        "approved_memory_count", "candidate_catalog_count", "registered_concept_count",
        "mechanical_object_count", "llm_package_count",
        "llm_case_count", "proposal_count", "coverage_object_count", "coverage_observation_count",
    ):
        left, right = before.get(field), after.get(field)
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            result[field] = right - left
    for field in ("object_status_counts", "state_status_counts"):
        keys = set(before.get(field) or {}) | set(after.get(field) or {})
        result[field] = {
            key: int((after.get(field) or {}).get(key, 0))
            - int((before.get(field) or {}).get(key, 0))
            for key in sorted(keys)
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Automatically promote evidence-backed concept types and rerun alignment epochs.",
    )
    parser.add_argument(
        "--seed-artifact-dir", default="outputs/semantic_alignment_content_v2_full_20260830",
        help="Existing full alignment used as epoch 0.",
    )
    parser.add_argument("--output-dir", default="outputs/semantic_alignment_auto_loop_20260831")
    parser.add_argument("--rule-output-dir", default="outputs/rule_extraction_full_20260829")
    parser.add_argument("--concept-output-dir", default="outputs/article_concepts_full_20260829")
    parser.add_argument("--context-output-dir", default="outputs/context_builder_semantic_qwen38_27b_20260829")
    parser.add_argument("--initial-reviewed-memory")
    parser.add_argument("--config-dir", default=str(ROOT / "configs" / "concept_layer"))
    parser.add_argument("--model-key", default="qwen3_8_27b")
    parser.add_argument("--max-epochs", type=int, default=2)
    parser.add_argument("--min-support", type=int, default=5)
    parser.add_argument("--review-batch-size", type=int, default=12)
    parser.add_argument("--workers", type=int)
    parser.add_argument("--proposal-threshold", type=int, default=5)
    parser.add_argument("--candidates-per-object", type=int, default=6)
    parser.add_argument("--objects-per-package", type=int, default=16)
    parser.add_argument("--max-package-chars", type=int, default=40_000)
    parser.add_argument("--refresh-reviews", action="store_true")
    parser.add_argument("--refresh-alignments", action="store_true")
    args = parser.parse_args()
    if args.max_epochs < 1 or args.min_support < 1:
        parser.error("--max-epochs and --min-support must be positive")

    seed = Path(args.seed_artifact_dir)
    if not (seed / "alignment_report.json").is_file():
        parser.error(f"seed artifact is missing alignment_report.json")
    try:
        seed_proposals = _proposals_path(seed)
    except FileNotFoundError:
        parser.error(f"seed artifact is missing alignment_proposals*.jsonl")
    output = Path(args.output_dir)
    if output.exists() and any(output.iterdir()):
        parser.error("output directory must be new or empty")
    output.mkdir(parents=True, exist_ok=True)

    inputs = load_alignment_inputs(args.rule_output_dir, args.concept_output_dir, args.context_output_dir)
    events = read_jsonl(args.initial_reviewed_memory) if args.initial_reviewed_memory else []
    models = yaml.safe_load((Path(args.config_dir) / "models.yaml").read_text(encoding="utf-8"))["models"]
    workers = args.workers or int(models[args.model_key].get("max_concurrency", 1))
    current_artifact = seed
    current_report = json.loads((seed / "alignment_report.json").read_text(encoding="utf-8"))
    seed_memory = MemorySnapshot.build(inputs.concepts, inputs.relations, events)
    current_report.setdefault("registered_concept_count", seed_memory.approved_memory_count)
    current_report.setdefault("candidate_catalog_count", sum(
        1 for row in seed_memory.concepts
        if row.get("registration_status") != "APPROVED"
    ))
    loop_report: dict[str, Any] = {
        "schema_version": "semantic_alignment.auto_loop.v1",
        "seed_artifact_dir": str(seed),
        "seed_role": "LEGACY_CANDIDATE_HARVEST_ONLY",
        "base_input_manifest": inputs.input_manifest,
        "policy": {
            "scope": "existing_candidate_identity_and_type",
            "approval": "MODEL_GATE_AND_DETERMINISTIC_GATE",
            "new_concepts": "DEFERRED_UNTIL_SOURCE_EVIDENCE_BINDING",
            "min_support": args.min_support,
            "max_epochs": args.max_epochs,
        },
        "initial": _report_summary(current_report),
        "epochs": [],
        "stop_reason": None,
    }
    write_json(output / "loop_report.json", loop_report)

    for epoch in range(1, args.max_epochs + 1):
        epoch_dir = output / f"epoch_{epoch:03d}"
        memory_before = MemorySnapshot.build(inputs.concepts, inputs.relations, events)
        proposals = read_jsonl(_proposals_path(current_artifact))
        candidates = build_concept_admission_candidates(
            proposals, memory_before, min_support=args.min_support,
        )
        write_jsonl(epoch_dir / "admission_candidates.jsonl", candidates)
        gate = ConceptAdmissionGate(
            models, args.model_key,
            ROOT / "prompts" / "semantic_alignment" / "concept_type_gate_v1.yaml",
            epoch_dir / "type_gate",
            workers=workers,
            batch_size=args.review_batch_size,
        )
        reviews, approved, gate_report = gate.run(
            candidates, memory_version=memory_before.version, refresh=args.refresh_reviews,
        )
        write_jsonl(epoch_dir / "admission_reviews.jsonl", reviews)
        write_jsonl(epoch_dir / "approved_events.jsonl", approved)
        epoch_row: dict[str, Any] = {
            "epoch": epoch,
            "source_artifact_dir": str(current_artifact),
            "memory_before": memory_before.version,
            "candidate_count": len(candidates),
            "gate": gate_report,
            "approved_event_count": len(approved),
        }
        if not approved:
            epoch_row["stop_reason"] = "NO_NEW_APPROVALS"
            loop_report["epochs"].append(epoch_row)
            loop_report["stop_reason"] = "NO_NEW_APPROVALS"
            write_json(output / "loop_report.json", loop_report)
            break

        events.extend(approved)
        write_jsonl(output / "reviewed_memory.jsonl", events)
        memory_after = MemorySnapshot.build(inputs.concepts, inputs.relations, events)
        alignment_dir = epoch_dir / "alignment"
        command = [
            sys.executable, str(ROOT / "scripts" / "align_semantics.py"),
            "--rule-output-dir", args.rule_output_dir,
            "--concept-output-dir", args.concept_output_dir,
            "--context-output-dir", args.context_output_dir,
            "--output-dir", str(alignment_dir),
            "--reviewed-memory", str(output / "reviewed_memory.jsonl"),
            "--config-dir", args.config_dir,
            "--model-key", args.model_key,
            "--max-tier", "H3",
            "--proposal-threshold", str(args.proposal_threshold),
            "--candidates-per-object", str(args.candidates_per_object),
            "--objects-per-package", str(args.objects_per_package),
            "--max-package-chars", str(args.max_package_chars),
            "--workers", str(workers),
        ]
        if args.refresh_alignments:
            command.append("--refresh")
        completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
        (epoch_dir / "alignment_stdout.json").write_text(completed.stdout, encoding="utf-8")
        (epoch_dir / "alignment_stderr.log").write_text(completed.stderr, encoding="utf-8")
        if completed.returncode:
            epoch_row.update({
                "memory_after": memory_after.version,
                "alignment_status": "FAILED",
                "alignment_returncode": completed.returncode,
            })
            loop_report["epochs"].append(epoch_row)
            loop_report["stop_reason"] = "ALIGNMENT_FAILED"
            write_json(output / "loop_report.json", loop_report)
            raise RuntimeError(f"alignment epoch {epoch} failed; see {epoch_dir / 'alignment_stderr.log'}")
        next_report = json.loads((alignment_dir / "alignment_report.json").read_text(encoding="utf-8"))
        before_summary = _report_summary(current_report)
        after_summary = _report_summary(next_report)
        epoch_row.update({
            "memory_after": memory_after.version,
            "alignment_status": "SUCCESS",
            "before": before_summary,
            "after": after_summary,
            "comparison_scope": (
                "LEGACY_SEED_NOT_REGISTRATION_COMPARABLE"
                if epoch == 1 else "SAME_REGISTRATION_MODEL"
            ),
        })
        if epoch > 1:
            epoch_row["delta"] = _delta(before_summary, after_summary)
        loop_report["epochs"].append(epoch_row)
        current_artifact = alignment_dir
        current_report = next_report
        write_json(output / "loop_report.json", loop_report)
    else:
        loop_report["stop_reason"] = "MAX_EPOCHS_REACHED"
        write_json(output / "loop_report.json", loop_report)

    print(json.dumps(loop_report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
