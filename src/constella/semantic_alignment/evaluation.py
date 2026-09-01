from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from typing import Any, Iterable

from .models import AlignmentStatus, PackageTier, SemanticRole, TIER_ORDER
from .packages import SemanticPackageBuilder


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(source)
    return [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]


def percentile(values: Iterable[int | float], fraction: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * fraction)))
    return round(ordered[index], 4)


def artifact_metrics(
    object_rows: list[dict[str, Any]],
    state_rows: list[dict[str, Any]],
    proposal_rows: list[dict[str, Any]],
    coverage_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    rule_states = [row for row in state_rows if row.get("semantic_role") == SemanticRole.RULE_VALUE]
    binding_by_role: dict[str, dict[str, Any]] = {}
    for role in SemanticRole:
        role_rows = [row for row in state_rows if row.get("semantic_role") == role]
        role_weight = sum(int(row.get("frequency") or 0) for row in role_rows)
        matched_rows = [
            row for row in role_rows
            if row.get("subject_binding_status") == AlignmentStatus.MATCHED
        ]
        matched_weight = sum(int(row.get("frequency") or 0) for row in matched_rows)
        binding_by_role[str(role)] = {
            "record_count": len(role_rows),
            "occurrence_count": role_weight,
            "status_counts": dict(Counter(
                str(row.get("subject_binding_status") or "UNKNOWN") for row in role_rows
            )),
            "bound_rate": round(len(matched_rows) / len(role_rows), 4) if role_rows else 0.0,
            "weighted_bound_rate": round(matched_weight / role_weight, 4) if role_weight else 0.0,
        }
    object_weight = sum(int(row.get("frequency") or 0) for row in object_rows)
    state_weight = sum(int(row.get("frequency") or 0) for row in rule_states)
    object_matched_weight = sum(
        int(row.get("frequency") or 0) for row in object_rows
        if row.get("alignment_status") == AlignmentStatus.MATCHED
    )
    state_bound_weight = sum(
        int(row.get("frequency") or 0) for row in rule_states
        if row.get("subject_binding_status") == AlignmentStatus.MATCHED
    )
    proposed_records = [row for row in [*object_rows, *state_rows] if row.get("proposal_id")]
    quantities = [row for row in state_rows if row.get("quantity")]
    converted = [
        row for row in quantities
        if row["quantity"].get("conversion_status") == "CONVERTED"
    ]
    return {
        "object_record_count": len(object_rows),
        "state_record_count": len(state_rows),
        "rule_value_count": len(rule_states),
        "derived_state_count": len(state_rows) - len(rule_states),
        "object_status_counts": dict(Counter(str(row.get("alignment_status")) for row in object_rows)),
        "state_subject_binding_status_counts": dict(Counter(
            str(row.get("subject_binding_status")) for row in rule_states
        )),
        "state_subject_binding_by_semantic_role": binding_by_role,
        "structure_counts": dict(Counter(str(row.get("structure")) for row in object_rows)),
        "semantic_role_counts": dict(Counter(str(row.get("semantic_role")) for row in state_rows)),
        "object_matched_rate": round(
            sum(row.get("alignment_status") == AlignmentStatus.MATCHED for row in object_rows)
            / len(object_rows), 4,
        ) if object_rows else 0.0,
        "object_weighted_matched_rate": round(object_matched_weight / object_weight, 4) if object_weight else 0.0,
        "state_subject_bound_rate": round(
            sum(row.get("subject_binding_status") == AlignmentStatus.MATCHED for row in rule_states)
            / len(rule_states), 4,
        ) if rule_states else 0.0,
        "state_weighted_subject_bound_rate": round(
            state_bound_weight / state_weight, 4,
        ) if state_weight else 0.0,
        "quantity_record_count": len(quantities),
        "quantity_conversion_count": len(converted),
        "proposal_count": len(proposal_rows),
        "proposal_counts": dict(Counter(str(row.get("proposal_kind")) for row in proposal_rows)),
        "proposal_priority_counts": dict(Counter(str(row.get("review_priority")) for row in proposal_rows)),
        "proposal_compression_rate": round(len(proposal_rows) / len(proposed_records), 4)
        if proposed_records else 0.0,
        "proposal_source_record_count": len(proposed_records),
        "coverage_object_count": len(coverage_rows),
        "coverage_observation_count": sum(len(row.get("observations") or []) for row in coverage_rows),
    }


def package_metrics(packages: list[dict[str, Any]]) -> dict[str, Any]:
    sizes = [len(json.dumps(package, ensure_ascii=False)) for package in packages]
    cases = [len(package.get("cases") or []) for package in packages]
    return {
        "package_count": len(packages),
        "case_count": sum(cases),
        "package_count_by_tier": dict(Counter(str(row.get("tier")) for row in packages)),
        "input_chars_total": int(sum(sizes)),
        "input_chars_p50": percentile(sizes, 0.5),
        "input_chars_p95": percentile(sizes, 0.95),
        "input_chars_max": max(sizes, default=0),
        "cases_per_package_average": round(sum(cases) / len(cases), 4) if cases else 0.0,
        "cases_per_package_p95": percentile(cases, 0.95),
    }


def load_weak_object_labels(
    path: str | Path,
    builder: SemanticPackageBuilder,
) -> dict[str, str]:
    """Load only old references that still point into the frozen memory.

    These are weak labels for candidate recall experiments, never semantic gold.
    """
    labels: dict[str, str] = {}
    for row in read_jsonl(path):
        object_id = str(row.get("object_id") or "")
        concept_id = str(row.get("concept_id") or "")
        if (
            row.get("decision") == "ALIGNED"
            and object_id in builder.object_rows
            and concept_id in builder.registry
        ):
            labels[object_id] = concept_id
    return labels


def candidate_recall_metrics(
    builder: SemanticPackageBuilder,
    packages: list[dict[str, Any]],
    weak_labels: dict[str, str],
) -> dict[str, Any]:
    candidates: dict[str, set[str]] = {
        str(case["object_id"]): {str(row["id"]) for row in case.get("candidates") or []}
        for package in packages for case in package.get("cases") or []
    }
    candidates.update({
        object_id: {
            str(core["concept_id"]) for core in row.get("core_objects") or []
            if core.get("concept_id")
        }
        for object_id, row in builder.mechanical_interpretations.items()
    })
    tier_by_id = {str(row["object_id"]): str(row["tier"]) for row in builder.scored_cases}
    evaluated = {
        object_id: concept_id for object_id, concept_id in weak_labels.items()
        if object_id in candidates
    }
    hits = {
        object_id for object_id, concept_id in evaluated.items()
        if concept_id in candidates[object_id]
    }
    total_weight = sum(int(builder.object_rows[object_id]["frequency"]) for object_id in evaluated)
    hit_weight = sum(int(builder.object_rows[object_id]["frequency"]) for object_id in hits)
    tier_rows: dict[str, dict[str, int]] = defaultdict(lambda: {"labels": 0, "hits": 0, "weight": 0, "hit_weight": 0})
    for object_id, concept_id in evaluated.items():
        tier = tier_by_id[object_id]
        weight = int(builder.object_rows[object_id]["frequency"])
        tier_rows[tier]["labels"] += 1
        tier_rows[tier]["weight"] += weight
        if concept_id in candidates[object_id]:
            tier_rows[tier]["hits"] += 1
            tier_rows[tier]["hit_weight"] += weight
    return {
        "label_source": "weak_previous_alignment_intersection",
        "weak_label_count": len(evaluated),
        "candidate_hit_count": len(hits),
        "candidate_recall": round(len(hits) / len(evaluated), 4) if evaluated else 0.0,
        "weighted_candidate_recall": round(hit_weight / total_weight, 4) if total_weight else 0.0,
        "by_tier": {
            tier: {
                **values,
                "recall": round(values["hits"] / values["labels"], 4) if values["labels"] else 0.0,
                "weighted_recall": round(values["hit_weight"] / values["weight"], 4) if values["weight"] else 0.0,
            }
            for tier, values in sorted(tier_rows.items(), key=lambda item: TIER_ORDER[PackageTier(item[0])])
        },
    }


def memory_gain_metrics(
    before: SemanticPackageBuilder,
    after: SemanticPackageBuilder,
) -> dict[str, Any]:
    before_tiers = {str(row["object_id"]): PackageTier(row["tier"]) for row in before.scored_cases}
    after_tiers = {str(row["object_id"]): PackageTier(row["tier"]) for row in after.scored_cases}
    common = set(before_tiers) & set(after_tiers)
    promoted = {
        object_id for object_id in common
        if TIER_ORDER[after_tiers[object_id]] < TIER_ORDER[before_tiers[object_id]]
    }
    promoted_weight = sum(int(after.object_rows[object_id]["frequency"]) for object_id in promoted)
    total_weight = sum(int(after.object_rows[object_id]["frequency"]) for object_id in common)
    return {
        "before_memory_version": before.memory.version,
        "after_memory_version": after.memory.version,
        "evaluated_object_count": len(common),
        "promoted_object_count": len(promoted),
        "tier_promotion_rate": round(len(promoted) / len(common), 4) if common else 0.0,
        "weighted_tier_promotion_rate": round(promoted_weight / total_weight, 4) if total_weight else 0.0,
        "mechanical_object_gain": len(after.mechanical_interpretations) - len(before.mechanical_interpretations),
    }


def gold_metrics(
    object_rows: list[dict[str, Any]],
    state_rows: list[dict[str, Any]],
    gold_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    predicted_objects = {str(row["source_state_id"]): row for row in object_rows}
    predicted_states = {_state_key(row): row for row in state_rows}
    gold_objects = {
        str(row["source_state_id"]): row for row in gold_rows
        if row.get("record_type") == "object"
    }
    gold_states = {
        _state_key(row): row for row in gold_rows
        if row.get("record_type") == "state"
    }
    structure_hits = sum(
        predicted_objects.get(key, {}).get("structure") == gold.get("structure")
        for key, gold in gold_objects.items()
    )
    core_tp = core_fp = core_fn = exact_core_hits = 0
    for key, gold in gold_objects.items():
        predicted = predicted_objects.get(key, {})
        predicted_ids = {
            str(row["concept_id"]) for row in predicted.get("core_objects") or [] if row.get("concept_id")
        }
        gold_ids = {str(value) for value in gold.get("core_concept_ids") or []}
        core_tp += len(predicted_ids & gold_ids)
        core_fp += len(predicted_ids - gold_ids)
        core_fn += len(gold_ids - predicted_ids)
        exact_core_hits += predicted_ids == gold_ids
    surface_hits = subject_binding_hits = operator_hits = quantity_hits = qualifier_hits = 0
    for key, gold in gold_states.items():
        predicted = predicted_states.get(key, {})
        surface_hits += predicted.get("canonical_surface") == gold.get("canonical_surface")
        predicted_subjects = {
            str(row["concept_id"])
            for row in predicted.get("subject_object_refs") or []
            if row.get("concept_id")
        }
        gold_subjects = {str(value) for value in gold.get("subject_concept_ids") or []}
        subject_binding_hits += predicted_subjects == gold_subjects
        operator_hits += (
            predicted.get("operator_family") == gold.get("operator_family")
            and (predicted.get("quantity") or {}).get("inclusive")
            == (gold.get("quantity") or {}).get("inclusive")
        )
        quantity_hits += _quantity_equal(predicted.get("quantity"), gold.get("quantity"))
        qualifier_hits += _canonical_json(predicted.get("qualifiers") or []) == _canonical_json(
            gold.get("qualifiers") or []
        )
    return {
        "gold_object_count": len(gold_objects),
        "gold_state_count": len(gold_states),
        "object_record_coverage": round(
            len(set(predicted_objects) & set(gold_objects)) / len(gold_objects), 4,
        ) if gold_objects else 0.0,
        "state_record_coverage": round(
            len(set(predicted_states) & set(gold_states)) / len(gold_states), 4,
        ) if gold_states else 0.0,
        "structure_accuracy": round(structure_hits / len(gold_objects), 4) if gold_objects else 0.0,
        "exact_core_set_accuracy": round(exact_core_hits / len(gold_objects), 4) if gold_objects else 0.0,
        "core_concept": _prf(core_tp, core_fp, core_fn),
        "state_surface_accuracy": round(surface_hits / len(gold_states), 4) if gold_states else 0.0,
        "state_subject_binding_accuracy": round(
            subject_binding_hits / len(gold_states), 4,
        ) if gold_states else 0.0,
        "operator_boundary_accuracy": round(operator_hits / len(gold_states), 4) if gold_states else 0.0,
        "quantity_accuracy": round(quantity_hits / len(gold_states), 4) if gold_states else 0.0,
        "qualifier_accuracy": round(qualifier_hits / len(gold_states), 4) if gold_states else 0.0,
    }


def _state_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("source_state_id") or ""),
        str(row.get("semantic_role") or ""),
        str(row.get("raw_object") or ""),
        str(row.get("raw_state") or ""),
    )


def _quantity_equal(left: Any, right: Any, *, tolerance: Decimal = Decimal("0.000001")) -> bool:
    if left is None or right is None:
        return left is right
    for field in ("unit_canonical", "inclusive"):
        if left.get(field) != right.get(field):
            return False
    for field in ("value", "lower", "upper"):
        left_value, right_value = left.get(field), right.get(field)
        if left_value is None or right_value is None:
            if left_value != right_value:
                return False
            continue
        try:
            if abs(Decimal(str(left_value)) - Decimal(str(right_value))) > tolerance:
                return False
        except InvalidOperation:
            if str(left_value) != str(right_value):
                return False
    return True


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _prf(tp: int, fp: int, fn: int) -> dict[str, Any]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }
