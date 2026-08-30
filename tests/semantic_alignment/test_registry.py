from __future__ import annotations

from constella.semantic_alignment.models import AlignmentStatus
from constella.semantic_alignment.registry import ConceptRegistry, MemorySnapshot


def test_untyped_exact_reference_requires_type_review():
    memory = MemorySnapshot.build([
        {"concept_id": "c1", "canonical_name": "焊接电流", "aliases": ["电流"]},
    ], [])
    registry = ConceptRegistry(memory)
    result = registry.resolve_exact("电流", concept_type="object")
    assert result["status"] == AlignmentStatus.TYPE_REVIEW
    assert result["concept_id"] == "c1"


def test_approved_type_memory_promotes_exact_match_without_mutating_source():
    concepts = [{"concept_id": "c1", "canonical_name": "焊接电流", "aliases": ["电流"]}]
    initial = MemorySnapshot.build(concepts, [])
    reviewed = MemorySnapshot.build(concepts, [], [{
        "status": "APPROVED",
        "proposal_kind": "TYPE_REVIEW",
        "concept_id": "c1",
        "type": "object",
    }])
    assert ConceptRegistry(initial).resolve_exact("电流", concept_type="object")["status"] == AlignmentStatus.TYPE_REVIEW
    assert ConceptRegistry(reviewed).resolve_exact("电流", concept_type="object")["status"] == AlignmentStatus.MATCHED
    assert "type" not in concepts[0]


def test_reviewed_alias_enters_next_memory_snapshot():
    concepts = [{"concept_id": "c1", "canonical_name": "电池组", "aliases": [], "type": "object"}]
    reviewed = MemorySnapshot.build(concepts, [], [{
        "status": "APPROVED",
        "proposal_kind": "ALIAS",
        "concept_id": "c1",
        "alias": "蓄电池组",
    }])
    result = ConceptRegistry(reviewed).resolve_exact("蓄电池组", concept_type="object")
    assert result["status"] == AlignmentStatus.MATCHED
    assert result["concept_id"] == "c1"
