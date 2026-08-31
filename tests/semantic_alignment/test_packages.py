from __future__ import annotations

from collections import Counter

from constella.semantic_alignment import AlignmentInputs, MemorySnapshot, SemanticPackageBuilder
from constella.semantic_alignment.models import PackageTier
from constella.semantic_alignment.packages import CharNgramIndex


def _inputs() -> AlignmentInputs:
    return AlignmentInputs(
        concepts=[
            {"concept_id": "current", "canonical_name": "焊接电流", "aliases": ["电流"], "type": "object", "registration_status": "APPROVED"},
            {"concept_id": "depth", "canonical_name": "熔深", "aliases": [], "type": "object", "registration_status": "APPROVED"},
            {"concept_id": "battery", "canonical_name": "电池组", "aliases": [], "type": "object", "registration_status": "APPROVED"},
            {"concept_id": "temperature", "canonical_name": "温度", "aliases": [], "type": "object", "registration_status": "APPROVED"},
            {"concept_id": "increase", "canonical_name": "增大", "aliases": ["提高"], "type": "state", "registration_status": "APPROVED"},
            {"concept_id": "charging", "canonical_name": "充电中", "aliases": ["正在充电"], "type": "state", "registration_status": "APPROVED"},
        ],
        relations=[],
        rules=[
            {
                "id": "r1", "context_package_id": "p1", "relation": "导致",
                "raw_expression": "电流|提高 —[导致]→ 熔深|增大", "conditions": [],
                "antecedents": [{"id": "s1", "object": "电流", "raw_state": "提高", "normalized_state": "提高"}],
                "consequents": [{"id": "s2", "object": "熔深", "raw_state": "增大", "normalized_state": "增大"}],
            },
            {
                "id": "r2", "context_package_id": "p2", "relation": "要求",
                "raw_expression": "温度超过60°C时充电中的电池组|安全检查", "conditions": [],
                "antecedents": [{
                    "id": "s3", "object": "温度超过60°C时充电中的电池组",
                    "raw_state": "安全检查", "normalized_state": "安全检查",
                }],
                "consequents": [],
            },
        ],
        context_packages={},
        units={},
    )


def test_ngram_index_returns_related_document_first():
    index = CharNgramIndex({"a": "焊接电流 回路电流", "b": "保护气体", "c": "焊接速度"})
    assert index.query("电流增大", top_k=2)[0][0] == "a"


def test_typed_exact_atomic_objects_bypass_llm():
    builder = SemanticPackageBuilder(_inputs())
    exact_ids = {
        row["object_id"] for row in builder.object_rows.values()
        if row["name"] in {"电流", "熔深"}
    }
    assert exact_ids <= set(builder.mechanical_interpretations)
    assert all(row["decision"] == "ATOMIC" for row in builder.mechanical_interpretations.values())


def test_packages_are_tier_homogeneous_stable_and_size_bounded():
    builder = SemanticPackageBuilder(_inputs())
    first = builder.object_alignment_packages(objects_per_package=2, max_package_chars=4_000)
    second = builder.object_alignment_packages(objects_per_package=2, max_package_chars=4_000)
    assert [row["package_id"] for row in first] == [row["package_id"] for row in second]
    assert all(len({case["tier"] for case in package["cases"]}) == 1 for package in first)
    assert all(package["tier"] in {PackageTier.H1, PackageTier.H2, PackageTier.H3} for package in first)
    assert builder.package_report(first)["max_package_chars"] < 6_000


def test_unprocessed_objects_are_banded_by_rank_one_five_twenty_five():
    rules = [{
        "id": f"r{index}", "context_package_id": f"p{index}",
        "conditions": [],
        "antecedents": [{
            "id": f"s{index}", "object": f"对象{index:02d}", "raw_state": "稳定",
        }],
        "consequents": [],
    } for index in range(31)]
    inputs = AlignmentInputs(
        concepts=[], relations=[], rules=rules, context_packages={}, units={},
    )
    builder = SemanticPackageBuilder(inputs, memory=MemorySnapshot.build([], []))

    counts = Counter(str(row["tier"]) for row in builder.scored_cases)

    assert counts == {PackageTier.H1: 1, PackageTier.H2: 5, PackageTier.H3: 25}
    cases = [case for package in builder.object_alignment_packages() for case in package["cases"]]
    assert sum(case["long_tail_fallback_required"] for case in cases) == 25
    assert all(
        case["tier"] == PackageTier.H3
        for case in cases if case["long_tail_fallback_required"]
    )


def test_frozen_priorities_do_not_shift_after_a_concept_is_registered():
    rules = [{
        "id": f"r{index}", "conditions": [],
        "antecedents": [{"id": f"s{index}", "object": f"对象{index:02d}", "raw_state": "稳定"}],
        "consequents": [],
    } for index in range(31)]
    inputs = AlignmentInputs(
        concepts=[], relations=[], rules=rules, context_packages={}, units={},
    )
    initial = SemanticPackageBuilder(inputs, memory=MemorySnapshot.build([], []))
    assignments = {
        row["object_id"]: {
            "rank_confidence": str(row["rank_confidence"]),
            "occurrence_rank": row["occurrence_rank"],
            "rank_population": row["rank_population"],
        }
        for row in initial.scored_cases
    }
    promoted = [{
        "concept_id": "promoted", "canonical_name": "对象00", "aliases": [],
        "type": "object", "registration_status": "APPROVED",
    }]
    rebuilt = SemanticPackageBuilder(
        inputs,
        memory=MemorySnapshot.build(promoted, []),
        priority_assignments=assignments,
    )

    assert any(
        interpretation["core_objects"][0]["concept_id"] == "promoted"
        for interpretation in rebuilt.mechanical_interpretations.values()
    )
    initial_tiers = {row["object_id"]: row["tier"] for row in initial.scored_cases}
    assert all(
        row["tier"] == initial_tiers[row["object_id"]]
        for row in rebuilt.scored_cases if row["tier"] != PackageTier.H0
    )


def test_object_alignment_input_pairs_each_object_with_its_rule_states():
    builder = SemanticPackageBuilder(_inputs())
    cases = [case for package in builder.object_alignment_packages() for case in package["cases"]]
    composite = next(case for case in cases if case["name"] == "温度超过60°C时充电中的电池组")

    assert composite["object_state_examples"] == [{
        "object": "温度超过60°C时充电中的电池组",
        "state": "安全检查",
        "expression": "温度超过60°C时充电中的电池组 | 安全检查",
        "frequency": 1,
    }]
    assert "state_examples" not in composite


def test_low_frequency_exact_match_is_not_forced_into_long_tail_fallback():
    inputs = AlignmentInputs(
        concepts=[{
            "concept_id": "combined", "canonical_name": "MIG/MAG焊", "aliases": [],
            "type": "object", "registration_status": "APPROVED",
        }],
        relations=[],
        rules=[{
            "id": "r1", "conditions": [],
            "antecedents": [{"id": "s1", "object": "MIG/MAG焊", "raw_state": "稳定"}],
            "consequents": [],
        }],
        context_packages={}, units={},
    )
    builder = SemanticPackageBuilder(inputs)

    assert builder.object_alignment_packages() == []
    interpretation = next(iter(builder.mechanical_interpretations.values()))
    assert interpretation["decision"] == "ATOMIC"
    assert interpretation["core_objects"][0]["concept_id"] == "combined"


def test_unapproved_exact_match_id_is_not_leaked_to_object_llm_package():
    inputs = AlignmentInputs(
        concepts=[{
            "concept_id": "pending_arc", "canonical_name": "电弧", "aliases": [],
            "type": "object", "registration_status": "CANDIDATE",
        }],
        relations=[],
        rules=[{
            "id": "r1", "conditions": [],
            "antecedents": [{"id": "s1", "object": "电弧", "raw_state": "稳定"}],
            "consequents": [],
        }],
        context_packages={}, units={},
    )
    package = SemanticPackageBuilder(inputs).object_alignment_packages()[0]
    case = package["cases"][0]

    assert case["candidates"] == []
    assert case["exact_resolution"]["concept_id"] is None
    assert "pending_arc" not in str(case)


def test_source_states_preserve_object_context_and_frequency():
    inputs = _inputs()
    inputs.rules.append({
        **inputs.rules[0],
        "id": "r3",
        "context_package_id": "p3",
    })
    builder = SemanticPackageBuilder(inputs)
    assert len(builder.state_rows) == 3
    assert builder.state_rows["s1"]["frequency"] == 2
    assert builder.source_occurrence_count == 5
    assert builder.state_rows["s1"]["raw_object"] == "电流"
    assert builder.state_rows["s1"]["raw_state"] == "提高"


def test_memory_version_changes_package_identity():
    inputs = _inputs()
    initial = MemorySnapshot.build(inputs.concepts, inputs.relations)
    reviewed = MemorySnapshot.build(inputs.concepts, inputs.relations, [{
        "status": "APPROVED",
        "proposal_kind": "ALIAS",
        "concept_id": "battery",
        "alias": "蓄电池组",
    }])
    initial_packages = SemanticPackageBuilder(inputs, memory=initial).object_alignment_packages()
    reviewed_packages = SemanticPackageBuilder(inputs, memory=reviewed).object_alignment_packages()
    assert initial.version != reviewed.version
    assert [row["package_id"] for row in initial_packages] != [row["package_id"] for row in reviewed_packages]
