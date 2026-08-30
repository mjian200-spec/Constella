from __future__ import annotations

from constella.semantic_alignment import AlignmentInputs, SemanticPackageBuilder, assemble_semantics
from constella.semantic_alignment.models import AlignmentStatus, SemanticRole, StructureStatus


def _builder() -> SemanticPackageBuilder:
    concepts = [
        {"concept_id": "current", "canonical_name": "焊接电流", "aliases": ["电流"], "type": "object"},
        {"concept_id": "depth", "canonical_name": "熔深", "aliases": [], "type": "object"},
        {"concept_id": "battery", "canonical_name": "电池组", "aliases": [], "type": "object"},
        {"concept_id": "temperature", "canonical_name": "温度", "aliases": [], "type": "object"},
        {"concept_id": "increase", "canonical_name": "增大", "aliases": ["提高"], "type": "state"},
        {"concept_id": "charging", "canonical_name": "充电中", "aliases": ["正在充电"], "type": "state"},
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


def test_proposals_are_thresholded_after_support_aggregation():
    builder = _builder()
    packages = builder.object_alignment_packages()
    selected = builder.selected_object_ids(packages, include_mechanical=True)
    _objects, _states, proposals, _coverage, _report = assemble_semantics(
        builder, packages, _compound_result(builder, packages),
        selected_object_ids=selected, proposal_threshold=5,
    )
    danger = next(row for row in proposals if row["canonical_name"] == "危险")
    assert danger["support"] == 5
    assert danger["proposal_kind"] == "STATE_CONCEPT"


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
    assert len([row for row in states if row["semantic_role"] == SemanticRole.RULE_VALUE]) == 1
    assert proposals
    assert all(report["invariants"].values())
