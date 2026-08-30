from __future__ import annotations

from constella.semantic_alignment.packages import AlignmentInputs, CharNgramIndex, SemanticPackageBuilder


def _inputs():
    return AlignmentInputs(
        concepts=[
            {"concept_id": "c1", "canonical_name": "GTAW", "aliases": ["TIG焊"], "definition": "钨极惰性气体保护焊"},
            {"concept_id": "c2", "canonical_name": "TIGW", "aliases": ["钨极氩气保护焊"], "definition": "使用钨极和氩气的焊接方法"},
            {"concept_id": "c3", "canonical_name": "焊接电流", "aliases": [], "definition": "焊接回路中的电流"},
        ],
        relations=[],
        rules=[{
            "id": "r1", "relation": "导致", "raw_expression": "提高电流导致熔深增大",
            "conditions": [],
            "antecedents": [{"id": "s1", "object": "电流", "raw_state": "提高", "normalized_state": "提高"}],
            "consequents": [{"id": "s2", "object": "熔深", "raw_state": "增大", "normalized_state": "增大"}],
        }],
        context_packages={},
        units={},
    )


def test_ngram_index_returns_related_document_first():
    index = CharNgramIndex({"a": "焊接电流 回路电流", "b": "保护气体", "c": "焊接速度"})
    assert index.query("电流增大", top_k=2)[0][0] == "a"


def test_package_ids_are_stable_and_objects_are_deduplicated():
    builder = SemanticPackageBuilder(_inputs())
    first = builder.concept_merge_packages(candidates_per_anchor=2, anchors_per_package=2)
    second = builder.concept_merge_packages(candidates_per_anchor=2, anchors_per_package=2)
    assert [item["package_id"] for item in first] == [item["package_id"] for item in second]
    assert len(builder.object_rows) == 2
    assert sum(len(item["cases"]) for item in builder.object_alignment_packages(objects_per_package=1)) == 2


def test_state_packages_include_full_minimal_rule_context():
    builder = SemanticPackageBuilder(_inputs())
    object_id = builder.object_rows["电流"]["object_id"]
    packages = builder.state_normalization_packages({object_id: "c3"})
    assert len(packages) == 1
    state = packages[0]["states"][0]
    assert state["id"] == "s1"
    assert state["contexts"][0]["relation"] == "导致"
    assert state["contexts"][0]["counterparts"] == ["熔深|增大"]


def test_state_clustering_keeps_near_equivalent_thresholds_together():
    states = [
        {"id": "a", "text": "大于120A时", "current_normalized": ">120 A"},
        {"id": "b", "text": "完全无关", "current_normalized": "完全无关"},
        {"id": "c", "text": "比120A大", "current_normalized": ">120 A"},
    ]
    chunks = SemanticPackageBuilder._cluster_states(states, 2)
    assert {item["id"] for item in chunks[0]} == {"a", "c"}


def test_merge_review_deduplicates_proposed_pairs():
    builder = SemanticPackageBuilder(_inputs())
    results = [{"status": "success", "output": {"merge_groups": [["c1", "c2"], ["c2", "c1"]]}}]
    packages = builder.concept_merge_review_packages(results)
    assert len(packages) == 1
    assert len(packages[0]["cases"]) == 1
    assert {packages[0]["cases"][0][side]["id"] for side in ("left", "right")} == {"c1", "c2"}


def test_object_and_concept_package_filters_select_only_requested_ids():
    builder = SemanticPackageBuilder(_inputs())
    concept_packages = builder.concept_merge_packages(anchor_ids={"c1"})
    assert len(concept_packages) == 1
    assert [case["anchor"]["id"] for case in concept_packages[0]["cases"]] == ["c1"]
    object_id = builder.object_rows["电流"]["object_id"]
    object_packages = builder.object_alignment_packages(object_ids={object_id})
    assert len(object_packages) == 1
    assert [case["object_id"] for case in object_packages[0]["cases"]] == [object_id]


def test_state_object_packages_expand_reparse_object_to_state_cases():
    builder = SemanticPackageBuilder(_inputs())
    object_id = builder.object_rows["电流"]["object_id"]
    packages = builder.state_object_alignment_packages({object_id})
    assert len(packages) == 1
    case = packages[0]["cases"][0]
    assert case["state_id"] == "s1"
    assert case["object_name"] == "电流"
    assert case["state_text"] == "提高"


def test_state_repair_packages_recall_candidates_for_composite_fragments():
    inputs = _inputs()
    inputs.concepts.extend([
        {"concept_id": "oil", "canonical_name": "油污", "aliases": [], "definition": None},
        {"concept_id": "rust", "canonical_name": "锈", "aliases": [], "definition": None},
    ])
    inputs.rules[0]["antecedents"][0].update({
        "object": "焊件状态", "raw_state": "油污、锈", "normalized_state": "油污、锈",
    })
    builder = SemanticPackageBuilder(inputs)
    packages = builder.state_repair_packages([{
        "state_id": "s1", "object_name": "焊件状态", "state_text": "油污、锈",
        "decision": "UNRESOLVED", "frequency": 2,
    }])
    names = {candidate["name"] for candidate in packages[0]["cases"][0]["candidates"]}
    assert {"油污", "锈"} <= names
