from constella.semantic_alignment.lifecycle import (
    LifecycleState,
    audit_concept_library,
    collect_unprocessed_objects,
    rank_by_occurrence,
)
from constella.semantic_alignment.registry import ConceptRegistry, MemorySnapshot


def test_occurrence_ranking_uses_one_five_twenty_five_population_ratio():
    rows = [
        {"candidate_id": f"c{index:02d}", "occurrence_count": 31 - index}
        for index in range(31)
    ]

    ranked = rank_by_occurrence(rows)

    assert [row["rank_confidence"] for row in ranked].count("HIGH") == 1
    assert [row["rank_confidence"] for row in ranked].count("MEDIUM") == 5
    assert [row["rank_confidence"] for row in ranked].count("LOW") == 25
    assert [row["occurrence_rank"] for row in ranked] == list(range(1, 32))


def test_occurrence_ranking_is_deterministic_at_ties():
    rows = [
        {"candidate_id": "beta", "occurrence_count": 3},
        {"candidate_id": "alpha", "occurrence_count": 3},
    ]

    assert [row["candidate_id"] for row in rank_by_occurrence(rows)] == ["alpha", "beta"]


def test_unprocessed_objects_exclude_registered_exact_matches():
    memory = MemorySnapshot.build([
        {
            "concept_id": "current", "canonical_name": "焊接电流", "aliases": ["电流"],
            "type": "object", "registration_status": "APPROVED",
        },
    ], [])
    registry = ConceptRegistry(memory)

    rows = collect_unprocessed_objects([
        {"object_id": "known", "name": "电流", "frequency": 8},
        {"object_id": "new", "name": "脉冲持续时间", "frequency": 3},
    ], registry)

    assert len(rows) == 1
    assert rows[0]["candidate_id"] == "new"
    assert rows[0]["occurrence_count"] == 3
    assert rows[0]["lifecycle_state"] == LifecycleState.UNPROCESSED_OBJECT


def test_library_audit_reports_term_collisions_missing_endpoints_and_cycles():
    memory = MemorySnapshot.build([
        {
            "concept_id": "a", "canonical_name": "电弧", "aliases": [],
            "type": "object", "registration_status": "APPROVED",
        },
        {
            "concept_id": "b", "canonical_name": "弧光", "aliases": ["电弧"],
            "type": "object", "registration_status": "APPROVED",
        },
    ], [
        {"relation_id": "r1", "child_concept_id": "a", "parent_concept_id": "b", "type": "IS_A"},
        {"relation_id": "r2", "child_concept_id": "b", "parent_concept_id": "a", "type": "IS_A"},
        {"relation_id": "r3", "child_concept_id": "a", "parent_concept_id": "missing", "type": "PART_OF"},
    ])

    report = audit_concept_library(memory)

    assert report["duplicate_term_collision_count"] == 1
    assert report["missing_registered_relation_endpoint_count"] == 1
    assert report["hierarchy_cycle_count"] == 1
    assert not all(report["invariants"].values())


def test_library_audit_defers_relations_until_both_concepts_are_registered():
    memory = MemorySnapshot.build([
        {"concept_id": "a", "canonical_name": "电弧", "aliases": []},
        {"concept_id": "b", "canonical_name": "放电", "aliases": []},
    ], [{
        "relation_id": "r1", "child_concept_id": "a",
        "parent_concept_id": "b", "type": "IS_A",
    }])

    report = audit_concept_library(memory)

    assert report["deferred_candidate_relation_count"] == 1
    assert report["missing_registered_relation_endpoint_count"] == 0
    assert all(report["invariants"].values())
