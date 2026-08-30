from __future__ import annotations

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
