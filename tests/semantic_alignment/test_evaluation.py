from __future__ import annotations

from constella.semantic_alignment import AlignmentInputs, MemorySnapshot, SemanticPackageBuilder
from constella.semantic_alignment.evaluation import (
    artifact_metrics,
    candidate_recall_metrics,
    gold_metrics,
    memory_gain_metrics,
    percentile,
)


def _builder(*, typed: bool = True) -> SemanticPackageBuilder:
    concepts = [{
        "concept_id": "current", "canonical_name": "焊接电流", "aliases": ["电流"],
        **({"type": "object", "registration_status": "APPROVED"} if typed else {}),
    }]
    inputs = AlignmentInputs(
        concepts=concepts,
        relations=[],
        rules=[{
            "id": "r1", "relation": "导致", "conditions": [], "consequents": [],
            "antecedents": [{
                "id": "s1", "object": "电流", "raw_state": "增大", "normalized_state": "增大",
            }],
        }],
        context_packages={},
        units={},
    )
    return SemanticPackageBuilder(inputs)


def test_percentile_is_stable_for_empty_and_nonempty_values():
    assert percentile([], 0.95) == 0.0
    assert percentile([1, 2, 100], 0.5) == 2.0


def test_artifact_metrics_use_frequency_and_compress_proposals():
    report = artifact_metrics(
        [{"frequency": 3, "alignment_status": "MATCHED", "structure": "ATOMIC"}],
        [
            {"frequency": 2, "semantic_role": "RULE_VALUE", "subject_binding_status": "MATCHED"},
            {"frequency": 1, "semantic_role": "RULE_CONDITION", "proposal_id": "p1"},
        ],
        [{"proposal_kind": "OBJECT_CONCEPT", "review_priority": "P1"}],
        [{"observations": [{}, {}]}],
    )
    assert report["object_weighted_matched_rate"] == 1.0
    assert report["derived_state_count"] == 1
    assert report["proposal_compression_rate"] == 1.0
    assert report["coverage_observation_count"] == 2


def test_candidate_recall_includes_mechanical_resolutions():
    builder = _builder()
    object_id = next(iter(builder.object_rows))
    report = candidate_recall_metrics(builder, [], {object_id: "current"})
    assert report["candidate_recall"] == 1.0
    assert report["weighted_candidate_recall"] == 1.0


def test_reviewed_type_promotes_untyped_exact_case():
    before = _builder(typed=False)
    reviewed = [{
        "status": "APPROVED",
        "concept": {
            "concept_id": "current", "canonical_name": "焊接电流",
            "aliases": ["电流"], "type": "object",
        },
    }]
    after = SemanticPackageBuilder(
        before.inputs,
        memory=MemorySnapshot.build(before.inputs.concepts, before.inputs.relations, reviewed),
    )
    report = memory_gain_metrics(before, after)
    assert report["promoted_object_count"] == 1
    assert report["tier_promotion_rate"] == 1.0


def test_gold_metrics_score_structure_core_state_and_quantity():
    object_rows = [{
        "source_state_id": "s1", "structure": "ATOMIC",
        "core_objects": [{"concept_id": "current"}],
    }]
    state_rows = [{
        "source_state_id": "s1", "semantic_role": "RULE_VALUE", "raw_object": "电流",
        "raw_state": "大于60A", "canonical_surface": "电流>{quantity}",
        "subject_object_refs": [{"concept_id": "current", "alignment_status": "MATCHED"}],
        "operator_family": ">", "qualifiers": [{"dimension": "电流"}],
        "quantity": {"value": "60", "unit_canonical": "A", "inclusive": False},
    }]
    gold = [
        {
            "record_type": "object", "source_state_id": "s1", "structure": "ATOMIC",
            "core_concept_ids": ["current"],
        },
        {
            "record_type": "state", "source_state_id": "s1", "semantic_role": "RULE_VALUE",
            "raw_object": "电流", "raw_state": "大于60A",
            "canonical_surface": "电流>{quantity}", "subject_concept_ids": ["current"],
            "operator_family": ">", "qualifiers": [{"dimension": "电流"}],
            "quantity": {"value": 60, "unit_canonical": "A", "inclusive": False},
        },
    ]
    report = gold_metrics(object_rows, state_rows, gold)
    assert report["exact_core_set_accuracy"] == 1.0
    assert report["core_concept"]["f1"] == 1.0
    assert report["state_surface_accuracy"] == 1.0
    assert report["state_subject_binding_accuracy"] == 1.0
    assert report["operator_boundary_accuracy"] == 1.0
    assert report["quantity_accuracy"] == 1.0
    assert report["qualifier_accuracy"] == 1.0
