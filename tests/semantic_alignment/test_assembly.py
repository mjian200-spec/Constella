from __future__ import annotations

from constella.semantic_alignment import AlignmentInputs, SemanticPackageBuilder, assemble_semantics
from constella.semantic_alignment.assembly import _state_coverage
from constella.semantic_alignment.models import AlignmentStatus, SemanticRole, StructureStatus


def _builder() -> SemanticPackageBuilder:
    concepts = [
        {"concept_id": "current", "canonical_name": "焊接电流", "aliases": ["电流"], "type": "object", "registration_status": "APPROVED"},
        {"concept_id": "depth", "canonical_name": "熔深", "aliases": [], "type": "object", "registration_status": "APPROVED"},
        {"concept_id": "battery", "canonical_name": "电池组", "aliases": [], "type": "object", "registration_status": "APPROVED"},
        {"concept_id": "temperature", "canonical_name": "温度", "aliases": [], "type": "object", "registration_status": "APPROVED"},
    ]
    rules = [{
        "id": "r1", "context_package_id": "p1", "relation": "导致",
        "raw_expression": "电流|提高 —[导致]→ 熔深|增大", "conditions": [],
        "antecedents": [{"id": "s1", "object": "电流", "raw_state": "提高", "normalized_state": "提高"}],
        "consequents": [{"id": "s2", "object": "熔深", "raw_state": "增大", "normalized_state": "增大"}],
    }]
    for index in range(5):
        rules.append({
            "id": f"r_compound_{index}", "context_package_id": "p2", "relation": "要求",
            "raw_expression": "温度超过60°C时充电中的电池组|危险 —[要求]→ 检查|执行",
            "conditions": [],
            "antecedents": [{
                "id": "s3", "object": "温度超过60°C时充电中的电池组",
                "raw_state": "危险", "normalized_state": "危险",
            }],
            "consequents": [],
        })
    return SemanticPackageBuilder(AlignmentInputs(
        concepts=concepts, relations=[], rules=rules, context_packages={}, units={},
    ))


def _compound_result(builder: SemanticPackageBuilder, packages):
    compound = next(
        row for row in builder.object_rows.values()
        if row["name"] == "温度超过60°C时充电中的电池组"
    )
    package = next(package for package in packages if any(
        case["object_id"] == compound["object_id"] for case in package["cases"]
    ))
    return [{
        "package_id": package["package_id"],
        "status": "success",
        "output": {"interpretations": [{
            "object_id": compound["object_id"],
            "decision": "DECOMPOSED",
            "core_objects": [{"text": "电池组", "concept_id": "battery"}],
            "embedded_states": [
                {"role": "OBJECT_INTRINSIC_STATE", "subject_text": "电池组", "state_text": "正在充电"},
                {"role": "RULE_CONDITION", "subject_text": "温度", "state_text": "超过60°C"},
            ],
            "qualifiers": [],
        }]},
    }]


def test_assembly_expands_template_without_losing_source_identity():
    builder = _builder()
    packages = builder.object_alignment_packages()
    selected = builder.selected_object_ids(packages, include_mechanical=True)
    objects, states, proposals, coverage, report = assemble_semantics(
        builder, packages, _compound_result(builder, packages),
        selected_object_ids=selected, proposal_threshold=5,
    )
    assert len(objects) == len(builder.state_rows) == 3
    rule_values = [row for row in states if row["semantic_role"] == SemanticRole.RULE_VALUE]
    assert len(rule_values) == 3
    compound_object = next(row for row in objects if row["source_state_id"] == "s3")
    assert compound_object["structure"] == StructureStatus.COMPOSED
    assert compound_object["alignment_status"] == AlignmentStatus.MATCHED
    assert len(compound_object["intrinsic_state_record_ids"]) == 1
    assert len(compound_object["condition_record_ids"]) == 1
    assert all(report["invariants"].values())
    assert report["concepts_created_by_alignment"] == 0
    assert coverage


def test_state_records_express_modifier_information_without_state_concepts():
    builder = _builder()
    packages = builder.object_alignment_packages()
    selected = builder.selected_object_ids(packages, include_mechanical=True)
    _objects, states, proposals, coverage, _report = assemble_semantics(
        builder, packages, _compound_result(builder, packages),
        selected_object_ids=selected, proposal_threshold=5,
    )
    assert all("state_concept_id" not in row for row in states)
    assert all("state_candidates" not in row for row in states)
    assert all("proposal_id" not in row for row in states)
    assert all("match_method" not in row for row in states)
    by_role = {
        row["semantic_role"]: row for row in states
    }
    assert by_role[SemanticRole.RULE_VALUE]["subject_binding_status"] == AlignmentStatus.MATCHED
    assert by_role[SemanticRole.OBJECT_INTRINSIC_STATE]["subject_binding_status"] == AlignmentStatus.MATCHED
    assert by_role[SemanticRole.RULE_CONDITION]["subject_binding_status"] == AlignmentStatus.MATCHED
    assert not proposals


def test_embedded_condition_is_parameterized_and_traceable():
    builder = _builder()
    packages = builder.object_alignment_packages()
    selected = builder.selected_object_ids(packages, include_mechanical=True)
    _objects, states, _proposals, _coverage, _report = assemble_semantics(
        builder, packages, _compound_result(builder, packages),
        selected_object_ids=selected, proposal_threshold=5,
    )
    condition = next(row for row in states if row["semantic_role"] == SemanticRole.RULE_CONDITION)
    assert condition["source_state_id"] == "s3"
    assert condition["raw_object"] == "温度"
    assert condition["raw_state"] == "超过60°C"
    assert condition["quantity"]["value"] == "333.15"
    assert condition["quantity"]["unit_canonical"] == "K"
    assert condition["frequency"] == 5


def test_state_coverage_groups_parameter_values_under_one_structured_expression():
    common = {
        "semantic_role": SemanticRole.RULE_VALUE,
        "canonical_surface": "电流>{quantity}", "operator_family": ">",
        "qualifiers": [{"dimension": "电流"}],
        "subject_binding_status": AlignmentStatus.MATCHED,
        "subject_object_refs": [{
            "concept_id": "current", "alignment_status": AlignmentStatus.MATCHED,
        }],
    }
    rows = [
        {
            **common, "source_state_id": "s60", "frequency": 2,
            "quantity": {"value": "60", "unit_canonical": "A", "inclusive": False},
        },
        {
            **common, "source_state_id": "s80", "frequency": 3,
            "quantity": {"value": "80", "unit_canonical": "A", "inclusive": False},
        },
    ]

    coverage = _state_coverage(rows)

    assert len(coverage) == 1
    assert coverage[0]["total_support"] == 5
    assert len(coverage[0]["observations"]) == 1
    assert len(coverage[0]["observations"][0]["parameter_observations"]) == 2


def _atomic_interpretation(builder: SemanticPackageBuilder, name: str) -> dict:
    row = next(row for row in builder.object_rows.values() if row["name"] == name)
    return {"status": "success", "output": {"interpretations": [{
        "object_id": row["object_id"], "decision": "ATOMIC",
        "core_objects": [{"text": name, "concept_id": None}],
        "embedded_states": [], "qualifiers": [],
    }]}}


def test_proposals_are_thresholded_after_support_aggregation():
    rules = []
    for index in range(5):
        rules.append({
            "id": f"r_joint_{index}", "relation": "要求", "conditions": [],
            "antecedents": [{
                "id": f"s_joint_{index}", "object": "异种材料接头",
                "raw_state": "强度", "normalized_state": "强度",
            }],
            "consequents": [],
        })
    for index in range(2):
        rules.append({
            "id": f"r_rod_{index}", "relation": "要求", "conditions": [],
            "antecedents": [{
                "id": f"s_rod_{index}", "object": "低氢型焊条",
                "raw_state": "干燥", "normalized_state": "干燥",
            }],
            "consequents": [],
        })
    builder = SemanticPackageBuilder(AlignmentInputs(
        concepts=[], relations=[], rules=rules, context_packages={}, units={},
    ))
    packages = builder.object_alignment_packages()
    selected = builder.selected_object_ids(packages, include_mechanical=True)
    _objects, _states, proposals, _coverage, _report = assemble_semantics(
        builder, packages,
        [_atomic_interpretation(builder, "异种材料接头"),
         _atomic_interpretation(builder, "低氢型焊条")],
        selected_object_ids=selected, proposal_threshold=5,
    )
    joint = next(row for row in proposals if row["canonical_name"] == "异种材料接头")
    assert joint["support"] == 5
    assert joint["proposal_kind"] == "OBJECT_CONCEPT"
    assert all(row["canonical_name"] != "低氢型焊条" for row in proposals)


def test_failed_package_falls_back_without_forcing_decomposition():
    builder = _builder()
    packages = builder.object_alignment_packages()
    compound = next(
        row for row in builder.object_rows.values()
        if row["name"] == "温度超过60°C时充电中的电池组"
    )
    package = next(package for package in packages if any(
        case["object_id"] == compound["object_id"] for case in package["cases"]
    ))
    objects, states, proposals, _coverage, report = assemble_semantics(
        builder, [package], [], selected_object_ids={compound["object_id"]}, proposal_threshold=5,
    )
    assert len(objects) == 1
    assert objects[0]["structure"] == StructureStatus.UNRESOLVED
    assert objects[0]["alignment_status"] == AlignmentStatus.PROPOSED
    rule_value = next(row for row in states if row["semantic_role"] == SemanticRole.RULE_VALUE)
    assert rule_value["subject_binding_status"] == AlignmentStatus.EXPRESSION_ONLY
    assert {row["proposal_kind"] for row in proposals} == {"OBJECT_CONCEPT"}
    assert all(report["invariants"].values())
