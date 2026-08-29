from __future__ import annotations

import json
from pathlib import Path
import tempfile

from constella.concept_layer.article_discovery import (
    ArticleDiscoveryRuntime,
    PackageConceptProcessor,
    _assemble,
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


def test_concept_package_runs_three_concept_calls_after_independent_role_call():
    values = {
        "package_role_classifier": {"is_rule_package": True, "is_concept_package": True},
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
    package = {"id": "p1", "core_unit_ids": ["u1"], "support_unit_ids": [], "asset_part_ids": [], "attributes": {}}
    result = PackageConceptProcessor(runtime(), client).process(package, {"u1": {"type": "passage", "content": "焊丝包括药芯焊丝", "attributes": {}}})
    assert result["attributes"]["roles"]["is_useless"] is False
    assert client.calls == ["package_role_classifier", "package_concept_extractor", "package_structure_extractor", "package_concept_auditor"]


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
        package, {"u1": {"type": "passage", "content": "焊丝包括药芯焊丝", "attributes": {}}},
    )
    assert client.calls == ["package_concept_extractor", "package_structure_extractor", "package_concept_auditor"]
    assert package["attributes"]["roles"]["status"] == "reused_context_builder_route"


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
            {"id": "p1", "core_unit_ids": ["u1"], "attributes": {}},
            {"id": "p2", "core_unit_ids": ["u2"], "attributes": {}},
        ]
        (source / "context_packages.jsonl").write_text(
            "".join(json.dumps(item) + "\n" for item in packages), encoding="utf-8",
        )
        client = SelectiveClient()
        report = run_article_concept_discovery(source, target, runtime(), client=client)
        rows = [json.loads(line) for line in (target / "context_packages.jsonl").read_text(encoding="utf-8").splitlines()]

    assert report["package_count"] == 2
    assert report["failed_package_count"] == 1
    assert client.calls == {"p1": 3, "p2": 1}
    assert rows[0]["attributes"]["concept_discovery"]["status"] == "failed"
    assert rows[1]["attributes"]["roles"]["is_useless"] is True
