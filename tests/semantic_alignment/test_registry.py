from __future__ import annotations

from constella.semantic_alignment.models import AlignmentStatus
from constella.semantic_alignment.registry import ConceptRegistry, MemorySnapshot


def test_unregistered_exact_reference_requires_concept_admission():
    memory = MemorySnapshot.build([
        {"concept_id": "c1", "canonical_name": "焊接电流", "aliases": ["电流"]},
    ], [])
    registry = ConceptRegistry(memory)
    result = registry.resolve_exact("电流", concept_type="object")
    assert result["status"] == AlignmentStatus.PROPOSED
    assert result["concept_id"] == "c1"


def test_full_concept_approval_promotes_exact_match_without_mutating_source():
    concepts = [{"concept_id": "c1", "canonical_name": "焊接电流", "aliases": ["电流"]}]
    initial = MemorySnapshot.build(concepts, [])
    type_only = MemorySnapshot.build(concepts, [], [{
        "status": "APPROVED",
        "proposal_kind": "TYPE_REVIEW",
        "concept_id": "c1",
        "type": "object",
    }])
    reviewed = MemorySnapshot.build(concepts, [], [{
        "status": "APPROVED",
        "concept": {
            "concept_id": "c1", "canonical_name": "焊接电流",
            "aliases": ["电流"], "type": "object",
        },
    }])
    assert ConceptRegistry(initial).resolve_exact("电流", concept_type="object")["status"] == AlignmentStatus.PROPOSED
    # An APPROVED type review is an explicit approval: it must resolve as registered.
    assert ConceptRegistry(type_only).resolve_exact("电流", concept_type="object")["status"] == AlignmentStatus.MATCHED
    assert ConceptRegistry(reviewed).resolve_exact("电流", concept_type="object")["status"] == AlignmentStatus.MATCHED
    assert "type" not in concepts[0]


def test_approved_merge_removes_source_and_remaps_relations():
    concepts = [
        {"concept_id": "target", "canonical_name": "电弧", "aliases": [], "type": "object", "registration_status": "APPROVED"},
        {"concept_id": "source", "canonical_name": "弧光", "aliases": [], "evidence_ids": ["u1"]},
        {"concept_id": "parent", "canonical_name": "放电", "aliases": [], "type": "object", "registration_status": "APPROVED"},
    ]
    memory = MemorySnapshot.build(concepts, [{
        "relation_id": "r1", "child_concept_id": "source",
        "parent_concept_id": "parent", "type": "IS_A",
    }], [{
        "status": "APPROVED", "proposal_kind": "CONCEPT_MERGE",
        "concept_id": "source", "target_concept_id": "target",
    }])

    assert {row["concept_id"] for row in memory.concepts} == {"target", "parent"}
    target = next(row for row in memory.concepts if row["concept_id"] == "target")
    assert "弧光" in target["aliases"]
    assert memory.relations[0]["child_concept_id"] == "target"


def test_approved_merge_does_not_steal_third_concept_term_as_alias():
    concepts = [
        {
            "concept_id": "gmaw", "canonical_name": "GMAW", "aliases": [],
            "type": "object", "registration_status": "APPROVED",
        },
        {
            "concept_id": "long_name", "canonical_name": "熔化极气体保护焊",
            "aliases": ["SGMAW"],
        },
        {
            "concept_id": "sgmaw", "canonical_name": "SGMAW", "aliases": [],
            "type": "object", "registration_status": "APPROVED",
        },
    ]

    memory = MemorySnapshot.build(concepts, [], [{
        "status": "APPROVED", "proposal_kind": "CONCEPT_MERGE",
        "concept_id": "long_name", "target_concept_id": "gmaw",
    }])
    by_id = {row["concept_id"]: row for row in memory.concepts}

    assert "熔化极气体保护焊" in by_id["gmaw"]["aliases"]
    assert "SGMAW" not in by_id["gmaw"]["aliases"]
    assert by_id["sgmaw"]["canonical_name"] == "SGMAW"


def test_reviewed_alias_enters_next_memory_snapshot():
    concepts = [{
        "concept_id": "c1", "canonical_name": "电池组", "aliases": [],
        "type": "object", "registration_status": "APPROVED",
    }]
    reviewed = MemorySnapshot.build(concepts, [], [{
        "status": "APPROVED",
        "proposal_kind": "ALIAS",
        "concept_id": "c1",
        "alias": "蓄电池组",
    }])
    result = ConceptRegistry(reviewed).resolve_exact("蓄电池组", concept_type="object")
    assert result["status"] == AlignmentStatus.MATCHED
    assert result["concept_id"] == "c1"


def test_candidate_fusion_prioritizes_name_alias_similarity_before_context():
    registry = ConceptRegistry(MemorySnapshot.build([
        {
            "concept_id": "arc", "canonical_name": "电弧", "aliases": [], "type": "object",
            "definition": "焊接电流形成的放电通道", "registration_status": "APPROVED",
        },
        {
            "concept_id": "source", "canonical_name": "焊接电源", "aliases": [], "type": "object",
            "definition": "焊接设备的供电装置", "registration_status": "APPROVED",
        },
    ], []))
    candidates = registry.candidates("焊接电弧", concept_type="object", top_k=2)
    assert candidates[0]["id"] == "arc"
    assert candidates[0]["match_method"] == "CONTAINED_NAME"


def test_unregistered_concepts_do_not_enter_object_candidate_or_coverage_indexes():
    registry = ConceptRegistry(MemorySnapshot.build([{
        "concept_id": "arc", "canonical_name": "电弧", "aliases": [],
        "type": "object", "registration_status": "CANDIDATE",
    }], []))

    assert registry.candidates("电弧", concept_type="object") == []
    assert registry.lexical_coverage("电弧", concept_type="object") == 0.0


def test_legacy_state_concepts_do_not_enter_object_identity_checks():
    registry = ConceptRegistry(MemorySnapshot.build([{
        "concept_id": "legacy_hot", "canonical_name": "过热", "aliases": ["高温"],
        "type": "state", "registration_status": "APPROVED",
    }], []))

    assert registry.registered_term_owners("过热") == []
    assert registry.identity_candidates("过热") == []
