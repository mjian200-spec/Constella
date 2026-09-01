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
    PackageTier,
    SemanticPackageBuilder,
    SerialConceptAdmissionRunner,
    audit_concept_library,
    build_initial_pending_concepts,
    build_pending_concepts_from_proposals,
    load_alignment_inputs,
    require_complete_alignment,
)
from constella.semantic_alignment.assembly import write_json, write_jsonl  # noqa: E402
from constella.semantic_alignment.concept_admission import (  # noqa: E402
    recall_concept_evidence,
)
from constella.semantic_alignment.evaluation import read_jsonl  # noqa: E402
from constella.semantic_alignment.registry import normalize_text  # noqa: E402


# Each cycle aligns its tier's objects against the current library and then
# admits, in the same cycle, the proposals that alignment raised. Cycle 0
# admits the extraction-stage catalog only; the final cycle re-reviews the
# deferred candidates with the now-complete library.
STAGE_PLAN: tuple[tuple[str, str | None], ...] = (
    ("INITIAL", None), ("H1", "H1"), ("H2", "H2"), ("H3", "H3"), (None, None),
)


def selected_stage_plan(max_cycles: int) -> tuple[tuple[str, str | None], ...]:
    if not 1 <= max_cycles <= len(STAGE_PLAN):
        raise ValueError(f"max_cycles must be between 1 and {len(STAGE_PLAN)}")
    return STAGE_PLAN[:max_cycles]


def _distribution(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
    return dict(sorted(Counter(str(row.get(field) or "UNKNOWN") for row in rows).items()))


def _supplement_review_history(
    events: list[dict[str, Any]], reviews: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Backfill non-accepted legacy reviews without changing the formal library."""
    represented = {str(row.get("source_review_id") or "") for row in events}
    result = list(events)
    for review in reviews:
        review_id = str(review.get("review_id") or "")
        if not review_id or review_id in represented or review.get("decision") == "FAILED":
            continue
        if review.get("accepted"):
            raise ValueError(f"accepted review is missing its approval event: {review_id}")
        decision = str(review.get("decision") or "NOT_ACCEPTED")
        result.append({
            "event_id": f"legacy_review_event_{review_id}",
            "status": decision if decision in {"DEFER", "REJECT"} else "NOT_ACCEPTED",
            "proposal_kind": "CONCEPT_REVIEW",
            "concept_id": str(review["concept_id"]),
            "canonical_name": str(review.get("canonical_name") or ""),
            "aliases": list(review.get("aliases") or []),
            "base_memory_version": review.get("memory_version"),
            "source_review_id": review_id,
            "decision": decision,
            "accepted": False,
            "gate_reason": review.get("gate_reason"),
            "model_decision": review.get("model_decision"),
            "legacy_backfill": True,
        })
        represented.add(review_id)
    return result


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


def _registered_library(
    memory: MemorySnapshot,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    concepts = [
        row for row in memory.concepts
        if row.get("registration_status") == "APPROVED"
        and str(row.get("type") or "") == "object"
    ]
    concept_ids = {str(row["concept_id"]) for row in concepts}
    relations = [
        row for row in memory.relations
        if row.get("registration_status") == "APPROVED"
        and str(row.get("child_concept_id") or "") in concept_ids
        and str(row.get("parent_concept_id") or "") in concept_ids
    ]
    candidates = [
        row for row in memory.concepts
        if row.get("registration_status") != "APPROVED"
        and str(row.get("type") or "") != "state"
    ]
    return concepts, relations, candidates


def _write_library_snapshot(directory: Path, memory: MemorySnapshot) -> None:
    concepts, relations, candidates = _registered_library(memory)
    write_jsonl(directory / "registered_concepts.jsonl", concepts)
    write_jsonl(directory / "registered_relations.jsonl", relations)
    write_jsonl(directory / "remaining_candidate_catalog.jsonl", candidates)


def _alignment_suffix(tier: str, object_limit: int | None) -> str:
    suffix = f"_{tier.lower()}"
    return f"{suffix}_trial_limit_{object_limit}" if object_limit is not None else suffix


def _run_alignment(
    args: argparse.Namespace,
    *,
    concept_dir: Path,
    reviewed_memory: Path,
    output_dir: Path,
    tier: str,
    priority_manifest: Path,
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
        "--tier", tier,
        "--priority-manifest", str(priority_manifest),
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
    suffix = _alignment_suffix(tier, args.object_limit)
    report_path = output_dir / f"alignment_report{suffix}.json"
    proposals_path = output_dir / f"alignment_proposals{suffix}.jsonl"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    require_complete_alignment(report)
    return report, read_jsonl(proposals_path)


def _parked_from_event(
    event: dict[str, Any],
    pending_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Turn a legacy DEFER memory event into a parked candidate for the final pass."""
    row = pending_by_id.get(str(event.get("concept_id") or ""))
    if row is None:
        return None
    parked = dict(row)
    parked["defer_history"] = [{
        "cycle": 0,
        "source": "INITIAL_MEMORY",
        "decision": "DEFER",
        "reason": event.get("reason") or event.get("gate_reason"),
    }]
    return parked


def _accumulate_parked(
    proposal_rows: list[dict[str, Any]],
    parked_rows: dict[str, dict[str, Any]],
    inputs,
) -> None:
    """Merge unique full-corpus evidence into parked DEFER candidates.

    Parked concepts are skipped by build_pending_concepts_from_proposals during
    the H1/H2/H3 cycles. Evidence is deduplicated by source-state identity; a
    later tier is another processing stage, not another occurrence.
    """
    supported_kinds = {"OBJECT_CONCEPT", "CONCEPT_APPROVAL", "TYPE_REVIEW"}
    parked_by_term: dict[str, list[dict[str, Any]]] = {}
    for parked in parked_rows.values():
        for value in [parked.get("canonical_name"), *(parked.get("aliases") or [])]:
            term = normalize_text(str(value or ""))
            if term:
                parked_by_term.setdefault(term, []).append(parked)
    for proposal in proposal_rows:
        if proposal.get("proposal_kind") not in supported_kinds:
            continue
        row = parked_rows.get(str(proposal.get("concept_id") or ""))
        if row is None:
            # Only deterministic identity is allowed here: an unambiguous
            # canonical-name or declared-alias owner. Near-synonyms stay
            # separate and are compared by the final model review.
            owners = parked_by_term.get(
                normalize_text(str(proposal.get("canonical_name") or "")),
                [],
            )
            row = owners[0] if len(owners) == 1 else None
        if row is None:
            continue
        source_state_ids = {
            *(row.get("source_state_ids") or []),
            *(proposal.get("source_state_ids") or []),
        }
        row["source_state_ids"] = sorted(source_state_ids)
        row["occurrence_count"] = len(source_state_ids)
        row["source_state_count"] = len(source_state_ids)
        source_rule_ids = {
            *(row.get("source_rule_ids") or []),
            *(proposal.get("source_rule_ids") or []),
        }
        row["source_rule_ids"] = sorted(source_rule_ids)
        row["source_rule_count"] = len(source_rule_ids)
        package_ids = {
            *(row.get("source_package_ids") or []),
            *(proposal.get("context_package_ids") or []),
        }
        evidence_ids = set(row.get("evidence_ids") or [])
        for package_id in package_ids:
            package = inputs.context_packages.get(str(package_id)) or {}
            evidence_ids.update(package.get("core_unit_ids") or [])
            evidence_ids.update(package.get("support_unit_ids") or [])
        row["source_package_ids"] = sorted(package_ids)
        row["evidence_ids"] = sorted(evidence_ids)
        row["evidence_count"] = len(evidence_ids)
        variant_sources = {
            str(expression): {str(value) for value in values if value}
            for expression, values in (
                row.get("raw_object_variant_source_ids") or {}
            ).items()
        }
        proposal_sources = {
            str(value) for value in proposal.get("source_state_ids") or [] if value
        }
        expression_sources = proposal.get("raw_expression_source_state_ids") or {}
        expressions = [str(value) for value in proposal.get("raw_expressions") or []]
        for expression in expressions:
            values = {
                str(value) for value in expression_sources.get(expression) or [] if value
            }
            if not values and len(expressions) == 1:
                values = proposal_sources
            variant_sources.setdefault(expression, set()).update(values)
        row["raw_object_variant_source_ids"] = {
            expression: sorted(values)
            for expression, values in sorted(variant_sources.items())
        }
        if variant_sources:
            row["raw_object_variants"] = [
                {"text": expression, "count": len(values)}
                for expression, values in sorted(
                    variant_sources.items(), key=lambda item: (-len(item[1]), item[0])
                )[:12]
            ]
        row["evidence"] = recall_concept_evidence({
            "canonical_name": row["canonical_name"],
            "aliases": row.get("aliases") or [],
            "evidence_ids": row["evidence_ids"],
        }, inputs.units)


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
        "final_state_subject_binding_status_counts": (
            assembly.get("state_subject_binding_status_counts") or {}
        ),
        "final_invariants": assembly.get("invariants") or {},
        "cycles": cycles,
    }


def _review_markdown(report: dict[str, Any]) -> str:
    audit = report["final_library_audit"]
    lines = [
        "# 概念生命周期最终审核总结",
        "",
        f"- 停止原因：`{report['stop_reason']}`",
        f"- 完成轮数：{report['cycle_count']}",
        f"- 最终记忆版本：`{report['final_memory_version']}`",
        f"- 已入库概念：{audit['registered_concept_count']}",
        f"- 剩余候选概念：{audit['candidate_concept_count']}",
        f"- 已批准关系：{sum(audit['relation_counts'].values())}",
        f"- 有正式关系的概念：{audit['registered_concepts_with_relations']}",
        f"- 孤立已入库概念：{audit['isolated_registered_concept_count']}",
        "",
        "## 正式库不变量",
        "",
    ]
    for name, passed in audit["invariants"].items():
        lines.append(f"- {'通过' if passed else '失败'}：`{name}`")
    lines.extend([
        "",
        "## 最终对象状态",
        "",
    ])
    for status, count in sorted(report["final_object_status_counts"].items()):
        lines.append(f"- `{status}`：{count}")
    lines.extend([
        "",
        "## 每轮变化",
        "",
        "| 轮次 | 新记忆事件 | 批准 | 合并 | 延后 | 拒绝 | 当轮候选 |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ])
    for row in report["cycles"]:
        admission = row.get("admission") or {}
        lines.append(
            f"| {row['cycle']} | {row.get('new_memory_event_count', 0)} | "
            f"{admission.get('approved_count', 0)} | {admission.get('merged_count', 0)} | "
            f"{admission.get('deferred_count', 0)} | {admission.get('rejected_count', 0)} | "
            f"{row.get('next_pending_concept_count', 0)} |"
        )
    return "\n".join(lines) + "\n"


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
    parser.add_argument(
        "--initial-review-history", action="append", default=[],
        help="Legacy admission_reviews JSONL to backfill DEFER/REJECT/non-accepted memory.",
    )
    parser.add_argument("--config-dir", default=str(ROOT / "configs" / "concept_layer"))
    parser.add_argument("--model-key", default="qwen3_8_27b")
    parser.add_argument(
        "--max-cycles", type=int, default=len(STAGE_PLAN),
        help="Stop after this lifecycle round (1-5); use --resume to continue later.",
    )
    parser.add_argument("--admission-limit", type=int, help="Smoke-test only: limit each admission pass.")
    parser.add_argument("--object-limit", type=int, help="Smoke-test only: limit LLM object packages.")
    parser.add_argument("--proposal-threshold", type=int, default=1)
    parser.add_argument("--candidates-per-object", type=int, default=8)
    parser.add_argument("--objects-per-package", type=int, default=12)
    parser.add_argument("--max-package-chars", type=int, default=40_000)
    parser.add_argument("--workers", type=int)
    parser.add_argument("--refresh-admissions", action="store_true")
    parser.add_argument("--refresh-alignments", action="store_true")
    parser.add_argument(
        "--resume", action="store_true",
        help="Reuse per-concept admission checkpoints and cached model results in an existing output directory.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        stage_plan = selected_stage_plan(args.max_cycles)
    except ValueError as error:
        parser.error(str(error))
    if args.admission_limit is not None and args.admission_limit < 1:
        parser.error("--admission-limit must be positive")
    if args.object_limit is not None and args.object_limit < 1:
        parser.error("--object-limit must be positive")

    output = Path(args.output_dir)
    if output.exists() and any(output.iterdir()) and not args.resume:
        parser.error("output directory must be new or empty")
    output.mkdir(parents=True, exist_ok=True)

    inputs = load_alignment_inputs(
        args.rule_output_dir, args.concept_output_dir, args.context_output_dir,
    )
    events = read_jsonl(args.initial_reviewed_memory) if args.initial_reviewed_memory else []
    for history_path in args.initial_review_history:
        events = _supplement_review_history(events, read_jsonl(history_path))
    catalog = [dict(row) for row in inputs.concepts]
    memory = MemorySnapshot.build(catalog, inputs.relations, events)
    pending = build_initial_pending_concepts(inputs, memory)
    reviewed_concept_ids: set[str] = {
        str(row["concept_id"]) for row in events
        if row.get("concept_id") and str(row.get("status") or "").upper() != "DEFER"
    }
    parked_rows: dict[str, dict[str, Any]] = {}
    pending_by_id = {str(row["concept_id"]): row for row in pending}
    for event in events:
        if str(event.get("status") or "").upper() != "DEFER":
            continue
        parked = _parked_from_event(event, pending_by_id)
        if parked is not None:
            parked_rows[str(parked["concept_id"])] = parked
    if parked_rows:
        pending = [row for row in pending if str(row["concept_id"]) not in parked_rows]
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
    priority_manifest = output / "object_priority_manifest.jsonl"
    stop_reason = "MAX_CYCLES_REACHED"
    for cycle_number, (admission_source, alignment_tier) in enumerate(stage_plan, start=1):
        cycle_dir = output / f"cycle_{cycle_number:03d}"
        final_pass = admission_source is None
        alignment_report = None
        if final_pass:
            # Final re-review: only the parked DEFER candidates remain, the
            # library is complete, and the review decides by recurrence
            # evidence and by synonym/near-synonym comparison against the
            # final library instead of the routine quality checks.
            pending = [dict(row) for row in parked_rows.values()]
            prompt_path = ROOT / "prompts" / "semantic_alignment" / "concept_final_review_v1.yaml"
        else:
            prompt_path = ROOT / "prompts" / "semantic_alignment" / "concept_admission_v2.yaml"
            if alignment_tier is not None:
                # Align this tier's objects against the current library first,
                # then admit the proposals that alignment raised in the same
                # cycle; cycle 0 admits the extraction-stage catalog as-is.
                concept_input = cycle_dir / "concept_input"
                _write_concept_input(concept_input, catalog, inputs.relations)
                alignment_report, proposals = _run_alignment(
                    args,
                    concept_dir=concept_input,
                    reviewed_memory=output / "reviewed_memory.jsonl",
                    output_dir=cycle_dir / "alignment",
                    tier=alignment_tier,
                    priority_manifest=priority_manifest,
                )
                # Terminal and parked names block fresh hash-id proposals, so a
                # name that was rejected or deferred under a catalog id cannot
                # re-enter pending under an alignment-side id.
                blocked_names = {
                    normalize_text(str(value))
                    for row in [
                        *[
                            event for event in events
                            if str(event.get("status") or "").upper() != "DEFER"
                        ],
                        *parked_rows.values(),
                    ]
                    for value in [row.get("canonical_name"), *(row.get("aliases") or [])]
                    if normalize_text(str(value or ""))
                }
                pending = build_pending_concepts_from_proposals(
                    proposals, inputs, memory,
                    reviewed_concept_ids=reviewed_concept_ids | set(parked_rows),
                    blocked_names=blocked_names,
                )
                _accumulate_parked(proposals, parked_rows, inputs)
        catalog = _catalog_with_candidates(catalog, pending)
        event_count_before = len(events)
        admission = SerialConceptAdmissionRunner(
            models, args.model_key, prompt_path, cycle_dir / "admission_cache",
            allow_defer=not final_pass,
        )
        reviews, events, admission_report = admission.run(
            pending,
            concepts=catalog,
            relations=inputs.relations,
            reviewed_memory=events,
            refresh=args.refresh_admissions,
            limit=args.admission_limit,
        )
        pending_by_id = {str(row["concept_id"]): row for row in pending}
        for review in reviews:
            concept_id = str(review["concept_id"])
            if review["status"] != "DEFER":
                reviewed_concept_ids.add(concept_id)
                continue
            candidate = pending_by_id.get(concept_id)
            if candidate is None:
                reviewed_concept_ids.add(concept_id)
                continue
            parked = dict(candidate)
            parked["defer_history"] = [
                *(candidate.get("defer_history") or []),
                {
                    "cycle": cycle_number,
                    "decision": "DEFER",
                    "reason": (
                        (review.get("model_decision") or {}).get("reason")
                        or review.get("gate_reason")
                    ),
                },
            ]
            parked_rows[concept_id] = parked
        write_jsonl(cycle_dir / "pending_concepts.jsonl", pending)
        write_jsonl(cycle_dir / "admission_reviews.jsonl", reviews)
        write_jsonl(output / "reviewed_memory.jsonl", events)

        memory = MemorySnapshot.build(catalog, inputs.relations, events)
        library_audit = audit_concept_library(memory)
        write_json(cycle_dir / "concept_library_audit.json", library_audit)
        _write_library_snapshot(cycle_dir, memory)
        if not all(library_audit["invariants"].values()):
            stop_reason = "CONCEPT_LIBRARY_INVARIANT_FAILED"
            cycles.append({
                "cycle": cycle_number, "admission": admission_report,
                "library_audit": library_audit, "stop_reason": stop_reason,
            })
            break

        if not priority_manifest.is_file():
            priority_builder = SemanticPackageBuilder(inputs, memory=memory)
            priority_rows = [{
                "object_id": row["object_id"],
                "name": row["name"],
                "frequency": row["frequency"],
                "tier": str(row["tier"]),
                "rank_confidence": str(row["rank_confidence"]),
                "occurrence_rank": row["occurrence_rank"],
                "rank_population": row.get("rank_population"),
            } for row in priority_builder.scored_cases if row["tier"] != PackageTier.H0]
            write_jsonl(priority_manifest, priority_rows)

        if final_pass:
            cycle_report = {
                "cycle": cycle_number,
                "admission_source": None,
                "alignment_tier": None,
                "new_memory_event_count": len(events) - event_count_before,
                "admission": admission_report,
                "library_audit": library_audit,
                "alignment": None,
                "final_rereview_count": len(parked_rows),
                "next_pending_concept_count": len(pending),
            }
            cycles.append(cycle_report)
            write_json(cycle_dir / "cycle_report.json", cycle_report)
            stop_reason = "ALL_TIERS_COMPLETED"
            break

        cycle_report = {
            "cycle": cycle_number,
            "admission_source": admission_source,
            "alignment_tier": alignment_tier,
            "new_memory_event_count": len(events) - event_count_before,
            "admission": admission_report,
            "library_audit": library_audit,
            "alignment": alignment_report,
            "parked_concept_count": len(parked_rows),
            "next_pending_concept_count": len(pending),
        }
        cycles.append(cycle_report)
        write_json(cycle_dir / "cycle_report.json", cycle_report)

    final_memory = MemorySnapshot.build(catalog, inputs.relations, events)
    final_report = _summary(
        cycles=cycles, events=events, memory=final_memory, stop_reason=stop_reason,
    )
    _write_library_snapshot(output / "final_library", final_memory)
    write_json(output / "lifecycle_report.json", final_report)
    (output / "final_review_summary.md").write_text(
        _review_markdown(final_report), encoding="utf-8",
    )
    print(json.dumps(final_report, ensure_ascii=False, indent=2))
    return 0 if stop_reason != "CONCEPT_LIBRARY_INVARIANT_FAILED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
