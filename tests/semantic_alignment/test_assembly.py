from __future__ import annotations

from constella.semantic_alignment import AlignmentInputs
from constella.semantic_alignment.assembly import (
    assemble_concepts,
    assemble_state_object_alignments,
    assemble_state_repairs,
)


def test_concept_assembly_skips_relation_with_missing_endpoint():
    inputs = AlignmentInputs(
        concepts=[{"concept_id": "c1", "canonical_name": "电流"}],
        relations=[{
            "child_concept_id": "c1", "parent_concept_id": "missing", "type": "IS_A",
        }],
        rules=[],
        context_packages={},
        units={},
    )
    concepts, relations, id_map, report = assemble_concepts(inputs, [])
    assert len(concepts) == 1
    assert relations == []
    assert id_map == {"c1": "c1"}
    assert report["missing_relation_endpoint_count"] == 1


def test_empty_state_object_assembly_does_not_report_false_success():
    rows, report = assemble_state_object_alignments([])
    assert rows == []
    assert report["state_alignment_rate"] == 0.0
    assert report["weighted_alignment_rate"] == 0.0


def test_state_repair_assembly_creates_derived_states_and_new_concepts():
    results = [{
        "status": "success",
        "_package": {"s1": {
            "object_name": "污染物", "state_text": "油、锈", "frequency": 3, "contexts": [],
        }},
        "output": {
            "repairs": [{"state_id": "s1", "parts": [
                {"concept_id": "oil", "object_name": "油污", "state_text": "含量较多"},
                {"concept_id": "NEW", "object_name": "锈蚀物", "state_text": "含量较多"},
            ]}],
            "unresolved_ids": [], "invalid_ids": [],
        },
    }]
    states, concepts, report = assemble_state_repairs(
        results, [{"concept_id": "oil", "canonical_name": "油污"}],
    )
    assert len(states) == 2
    assert len(concepts) == 2
    assert all(row["derived_state_id"] for row in states)
    assert report["repaired_source_count"] == 1
    assert report["new_concept_count"] == 1
