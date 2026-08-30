from __future__ import annotations

from constella.semantic_alignment import AlignmentInputs
from constella.semantic_alignment.assembly import (
    assemble_concepts,
    assemble_state_object_alignments,
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
