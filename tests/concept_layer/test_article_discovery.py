from __future__ import annotations

import json
from pathlib import Path
import tempfile

from constella.concept_layer.article_discovery import (
    ArticleDiscoveryRuntime,
    PackageConceptProcessor,
    _assemble,
    _package_payload,
    run_article_concept_discovery,
)


class FakeClient:
    def __init__(self, values):
        self.values = values
        self.calls = []

    def complete(self, model_key, messages, **kwargs):
        prompt_id = kwargs["prompt_id"]
        self.calls.append(prompt_id)
        return {"model": "fake", "choices": [{"message": {"content": json.dumps(self.values[prompt_id], ensure_ascii=False)}}]}


def runtime():
    return ArticleDiscoveryRuntime("fake", {"fake": {"model": "fake"}}, Path("prompts/concept_layer"), use_llm=True)


def _graph(units, constraints=None):
    return {"units": units, "constraints": constraints or {}, "ambiguities": {}}


def test_concept_package_runs_three_calls_after_reusing_context_builder_route():
    values = {
        "package_concept_extractor": {"concepts": [
            {"name": "药芯焊丝", "explicit_definition": None, "name_evidence_unit_ids": ["u1"], "definition_evidence_unit_ids": []},
            {"name": "焊丝", "explicit_definition": None, "name_evidence_unit_ids": ["u1"], "definition_evidence_unit_ids": []},
        ]},
        "package_structure_extractor": {"relations": [{"relation_type": "HAS_SUBTYPE", "source": "焊丝", "target": "药芯焊丝", "evidence_unit_ids": ["u1"], "evidence_text": "焊丝包括药芯焊丝"}]},
        "package_concept_auditor": {"status": "accepted", "issues": [], "concepts": [
            {"name": "药芯焊丝", "explicit_definition": None, "name_evidence_unit_ids": ["u1"], "definition_evidence_unit_ids": []},
            {"name": "焊丝", "explicit_definition": None, "name_evidence_unit_ids": ["u1"], "definition_evidence_unit_ids": []},
        ], "relations": [{"relation_type": "HAS_SUBTYPE", "source": "焊丝", "target": "药芯焊丝", "evidence_unit_ids": ["u1"], "evidence_text": "焊丝包括药芯焊丝"}]},
    }
    client = FakeClient(values)
    package = {"id": "p1", "core_unit_ids": ["u1"], "support_unit_ids": [], "asset_part_ids": [], "attributes": {"package_role": {
        "status": "ok", "is_rule_package": True, "is_concept_package": True, "is_useless": False,
    }}}
    result = PackageConceptProcessor(runtime(), client).process(package, _graph({"u1": {"type": "passage", "content": "焊丝包括药芯焊丝", "attributes": {}}}))
    assert result["attributes"]["roles"]["is_useless"] is False
    assert client.calls == ["package_concept_extractor", "package_structure_extractor", "package_concept_auditor"]


def test_context_builder_package_route_is_reused_without_a_second_role_call():
    values = {
        "package_concept_extractor": {"concepts": []},
        "package_structure_extractor": {"relations": []},
        "package_concept_auditor": {"status": "accepted", "issues": [], "concepts": [], "relations": []},
    }
    client = FakeClient(values)
    package = {
        "id": "p1", "core_unit_ids": ["u1"], "support_unit_ids": [], "asset_part_ids": [],
        "attributes": {"package_role": {
            "status": "ok", "is_rule_package": False, "is_concept_package": True,
            "is_useless": False, "prompt_id": "context_package_role_classifier", "prompt_version": "1.0",
        }},
    }
    PackageConceptProcessor(runtime(), client).process(
        package, _graph({"u1": {"type": "passage", "content": "焊丝包括药芯焊丝", "attributes": {}}}),
    )
    assert client.calls == ["package_concept_extractor", "package_structure_extractor", "package_concept_auditor"]
    assert package["attributes"]["roles"]["status"] == "reused_context_builder_route"


def test_payload_preserves_package_groups_conditions_and_resource_textualization():
    graph = _graph(
        {
            "u1": {"type": "passage", "content": "核心", "attributes": {}},
            "u2": {"type": "passage", "content": "支撑", "attributes": {}},
            "u3": {"type": "figure", "content": "图题", "attributes": {"resource_understanding": {"description": "文字化"}}},
            "u4": {"type": "passage", "content": "条件来源", "attributes": {}},
        },
        {"c1": {"type": "condition", "value": "室温", "scope": {"start_unit_id": "u1"}, "status": "certain", "source_id": "u4"}},
    )
    package = {"id": "p1", "core_unit_ids": ["u1"], "support_unit_ids": ["u2"], "asset_part_ids": ["u3"], "constraint_ids": ["c1"], "unresolved_ids": [], "attributes": {"section_path": ["章"], "package_role": {"status": "ok", "label": "concept"}}}
    payload = _package_payload(package, graph)
    assert payload["section_path"] == ["章"]
    assert [item["unit_id"] for item in payload["core_units"]] == ["u1"]
    assert [item["unit_id"] for item in payload["support_units"]] == ["u2"]
    assert payload["resources"][0]["resource_understanding"]["description"] == "文字化"
    assert payload["constraints"][0]["source_unit"]["unit_id"] == "u4"
    assert set(payload["evidence_unit_ids"]) == {"u1", "u2", "u3", "u4"}


def test_assembly_normalizes_reverse_relation_and_keeps_trace():
    packages = [{"id": "p1", "concept_extraction": {"audit": {"status": "accepted", "concepts": [
        {"name": "焊丝", "explicit_definition": None, "name_evidence_unit_ids": ["u1"], "definition_evidence_unit_ids": []},
        {"name": "药芯焊丝", "explicit_definition": None, "name_evidence_unit_ids": ["u1"], "definition_evidence_unit_ids": []},
    ], "relations": [{"relation_type": "HAS_SUBTYPE", "source": "焊丝", "target": "药芯焊丝", "evidence_unit_ids": ["u1"]}]}}}]
    concepts, relations = _assemble(packages)
    names = {item["concept_id"]: item["canonical_name"] for item in concepts}
    assert len(relations) == 1
    assert relations[0]["type"] == "IS_A"
    assert names[relations[0]["child_concept_id"]] == "药芯焊丝"
    assert names[relations[0]["parent_concept_id"]] == "焊丝"
    assert relations[0]["original_relation"]["relation_type"] == "HAS_SUBTYPE"


def test_same_as_alias_remains_resolvable_by_other_relations():
    concepts = [
        {"name": name, "explicit_definition": None, "name_evidence_unit_ids": ["u1"], "definition_evidence_unit_ids": []}
        for name in ("金属惰性气体焊", "MIG焊", "熔化极气体保护焊")
    ]
    packages = [{"id": "p1", "concept_extraction": {"audit": {"status": "accepted", "concepts": concepts, "relations": [
        {"relation_type": "SAME_AS", "source": "金属惰性气体焊", "target": "MIG焊", "evidence_unit_ids": ["u1"]},
        {"relation_type": "IS_A", "source": "金属惰性气体焊", "target": "熔化极气体保护焊", "evidence_unit_ids": ["u1"]},
    ]}}}]
    merged, relations = _assemble(packages)
    assert len(merged) == 2
    assert len(relations) == 1


def test_same_as_chains_are_order_independent():
    concepts = [
        {"name": name, "explicit_definition": None, "name_evidence_unit_ids": ["u1"], "definition_evidence_unit_ids": []}
        for name in ("A", "B", "C")
    ]

    def assemble(alias_pairs):
        relations = [
            {"relation_type": "SAME_AS", "source": source, "target": target, "evidence_unit_ids": ["u1"]}
            for source, target in alias_pairs
        ]
        package = {"id": "p1", "concept_extraction": {"audit": {
            "status": "accepted", "concepts": concepts, "relations": relations,
        }}}
        return _assemble([package])[0]

    forward = assemble([("A", "B"), ("B", "C")])
    reversed_order = assemble([("B", "C"), ("A", "B")])
    assert [(item["canonical_name"], item["aliases"]) for item in forward] == [
        (item["canonical_name"], item["aliases"]) for item in reversed_order
    ]
    assert forward[0]["canonical_name"] == "C"
    assert set(forward[0]["aliases"]) == {"A", "B"}


def test_duplicate_relations_merge_all_evidence_and_package_provenance():
    def package(package_id, unit_id):
        concepts = [
            {"name": name, "explicit_definition": None, "name_evidence_unit_ids": [unit_id], "definition_evidence_unit_ids": []}
            for name in ("子类", "父类")
        ]
        relation = {"relation_type": "IS_A", "source": "子类", "target": "父类", "evidence_unit_ids": [unit_id]}
        return {"id": package_id, "concept_extraction": {"audit": {
            "status": "accepted", "concepts": concepts, "relations": [relation],
        }}}

    _, relations = _assemble([package("p1", "u1"), package("p2", "u2")])
    assert len(relations) == 1
    assert relations[0]["source_package_ids"] == ["p1", "p2"]
    assert relations[0]["evidence_ids"] == ["u1", "u2"]
    assert len(relations[0]["original_relations"]) == 2


def test_hierarchy_drops_cycles_and_transitive_is_a_edges():
    names = ["A", "B", "C"]
    concepts = [{"name": name, "explicit_definition": None, "name_evidence_unit_ids": ["u1"], "definition_evidence_unit_ids": []} for name in names]
    relations = [
        {"relation_type": "IS_A", "source": "A", "target": "B", "evidence_unit_ids": ["u1"]},
        {"relation_type": "IS_A", "source": "B", "target": "C", "evidence_unit_ids": ["u1"]},
        {"relation_type": "IS_A", "source": "A", "target": "C", "evidence_unit_ids": ["u1"]},
        {"relation_type": "IS_A", "source": "C", "target": "A", "evidence_unit_ids": ["u1"]},
    ]
    _, result = _assemble([{"id": "p1", "concept_extraction": {"audit": {"status": "accepted", "concepts": concepts, "relations": relations}}}])
    assert len(result) == 2


def test_transport_errors_retry_and_one_failed_package_does_not_abort_the_run():
    class SelectiveClient:
        def __init__(self):
            self.calls = {"p1": 0, "p2": 0}

        def complete(self, model_key, messages, **kwargs):
            package_id = json.loads(messages[1]["content"])["package_id"]
            self.calls[package_id] += 1
            if package_id == "p1":
                raise TimeoutError("temporary model timeout")
            value = {"is_rule_package": False, "is_concept_package": False}
            return {"model": "fake", "choices": [{"message": {"content": json.dumps(value)}}]}

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source, target = root / "context", root / "article"
        source.mkdir()
        (source / "document_graph.json").write_text(json.dumps({
            "units": {
                "u1": {"type": "passage", "content": "A", "attributes": {}},
                "u2": {"type": "passage", "content": "B", "attributes": {}},
            }
        }), encoding="utf-8")
        packages = [
            {"id": "p1", "core_unit_ids": ["u1"], "attributes": {"package_role": {"status": "ok", "is_rule_package": False, "is_concept_package": True, "is_useless": False}}},
            {"id": "p2", "core_unit_ids": ["u2"], "attributes": {"package_role": {"status": "ok", "is_rule_package": False, "is_concept_package": False, "is_useless": True}}},
        ]
        (source / "context_packages.jsonl").write_text(
            "".join(json.dumps(item) + "\n" for item in packages), encoding="utf-8",
        )
        client = SelectiveClient()
        report = run_article_concept_discovery(source, target, runtime(), client=client)
        rows = [json.loads(line) for line in (target / "context_packages.jsonl").read_text(encoding="utf-8").splitlines()]
        package_results = {path.name: json.loads(path.read_text(encoding="utf-8")) for path in (target / "packages").glob("*.json")}

    assert report["package_count"] == 2
    assert report["failed_package_count"] == 1
    assert client.calls == {"p1": 3, "p2": 0}
    assert rows[0]["attributes"]["concept_discovery"]["status"] == "failed"
    assert rows[1]["attributes"]["roles"]["is_useless"] is True
    assert set(package_results) == {"p1.json", "p2.json"}
    assert package_results["p1.json"]["attributes"]["concept_discovery"]["status"] == "failed"
    assert package_results["p2.json"]["attributes"]["roles"]["is_useless"] is True


def test_resume_reuses_successful_package_results_and_reruns_failed():
    values = {
        "package_concept_extractor": {"concepts": [
            {"name": "焊丝", "explicit_definition": None, "name_evidence_unit_ids": ["u2"], "definition_evidence_unit_ids": []},
        ]},
        "package_structure_extractor": {"relations": []},
        "package_concept_auditor": {"status": "accepted", "issues": [], "concepts": [
            {"name": "焊丝", "explicit_definition": None, "name_evidence_unit_ids": ["u2"], "definition_evidence_unit_ids": []},
        ], "relations": []},
    }

    class CountingClient:
        def __init__(self):
            self.calls = []

        def complete(self, model_key, messages, **kwargs):
            package_id = json.loads(messages[1]["content"])["package_id"]
            self.calls.append(package_id)
            prompt_id = kwargs["prompt_id"]
            return {"model": "fake", "choices": [{"message": {"content": json.dumps(values[prompt_id], ensure_ascii=False)}}]}

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source, target = root / "context", root / "article"
        source.mkdir()
        (source / "document_graph.json").write_text(json.dumps({
            "units": {
                "u1": {"type": "passage", "content": "A", "attributes": {}},
                "u2": {"type": "passage", "content": "B", "attributes": {}},
            }
        }), encoding="utf-8")
        packages = [
            {"id": "p1", "core_unit_ids": ["u1"], "attributes": {"package_role": {"status": "ok", "is_rule_package": False, "is_concept_package": True, "is_useless": False}}},
            {"id": "p2", "core_unit_ids": ["u2"], "attributes": {"package_role": {"status": "ok", "is_rule_package": False, "is_concept_package": True, "is_useless": False}}},
            {"id": "p3", "core_unit_ids": ["u2"], "attributes": {"package_role": {"status": "ok", "is_rule_package": False, "is_concept_package": True, "is_useless": False}}},
        ]
        (source / "context_packages.jsonl").write_text(
            "".join(json.dumps(item) + "\n" for item in packages), encoding="utf-8",
        )
        (target / "packages").mkdir(parents=True)
        (target / "packages" / "p1.json").write_text(json.dumps({
            "id": "p1", "core_unit_ids": ["u1"],
            "attributes": {"roles": {"status": "reused_context_builder_route", "is_concept_package": True, "is_rule_package": False, "is_useless": False}, "concept_discovery": {"status": "ok"}},
            "concept_extraction": {"concepts": [], "relations": [], "audit": {"status": "accepted", "concepts": [], "relations": []}},
        }), encoding="utf-8")
        (target / "packages" / "p2.json").write_text(json.dumps({
            "id": "p2", "core_unit_ids": ["u2"],
            "attributes": {"concept_discovery": {"status": "failed", "reason": "previous model timeout"}},
        }), encoding="utf-8")
        client = CountingClient()
        report = run_article_concept_discovery(source, target, runtime(), client=client)
        rows = [json.loads(line) for line in (target / "context_packages.jsonl").read_text(encoding="utf-8").splitlines()]

    assert sorted(set(client.calls)) == ["p2", "p3"]
    assert rows[0]["id"] == "p1"
    assert rows[0]["attributes"]["concept_discovery"]["status"] == "ok"
    assert rows[0]["concept_extraction"]["audit"]["status"] == "accepted"
    assert rows[1]["id"] == "p2"
    assert rows[1]["concept_extraction"]["audit"]["status"] == "accepted"
    assert report["failed_package_count"] == 0
