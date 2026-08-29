from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from constella.knowledge_graph.importer import GraphImportError, load_graph_dataset


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


class KnowledgeGraphImporterTests(unittest.TestCase):
    def load(self, concepts: list[dict], relations: list[dict], rules: list[dict]):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        rule_dir, concept_dir = root / "rules", root / "concepts"
        rule_dir.mkdir(); concept_dir.mkdir()
        write_jsonl(rule_dir / "structured_rules.jsonl", rules)
        write_jsonl(concept_dir / "concepts.jsonl", concepts)
        write_jsonl(concept_dir / "concept_relations.jsonl", relations)
        return temporary, load_graph_dataset(rule_dir, concept_dir, "test")

    def test_state_expression_keeps_object_and_links_directly_to_concept(self):
        concepts = [{"concept_id": "c1", "canonical_name": "气体保护焊", "aliases": ["GMAW"]}]
        rules = [{
            "id": "r1", "context_package_id": "p1", "rule_group_id": "g1", "rule_index": 1,
            "relation": "提高", "raw_expression": "...", "conditions": [],
            "antecedents": [{"id": "s1", "object": "GMAW", "raw_state": "工效", "normalized_state": "工效"}],
            "consequents": [{"id": "s2", "object": "数值", "raw_state": "高", "normalized_state": "高"}],
            "transitions": [],
        }]
        temporary, dataset = self.load(concepts, [], rules)
        self.addCleanup(temporary.cleanup)
        states = {row["state_id"]: row for row in dataset.states}
        self.assertEqual("GMAW", states["s1"]["object"])
        self.assertEqual("matched", states["s1"]["concept_match_status"])
        self.assertEqual("unmatched", states["s2"]["concept_match_status"])
        self.assertEqual("exact_alias", dataset.concept_bindings[0]["match_method"])
        self.assertNotIn("object_terms", dataset.counts())

    def test_ambiguous_name_does_not_create_binding(self):
        concepts = [
            {"concept_id": "c1", "canonical_name": "概念一", "aliases": ["共享名"]},
            {"concept_id": "c2", "canonical_name": "概念二", "aliases": ["共享名"]},
        ]
        rules = [{
            "id": "r1", "conditions": [],
            "antecedents": [{"id": "s1", "object": "共享 名", "raw_state": "状态", "normalized_state": "状态"}],
            "consequents": [], "transitions": [],
        }]
        temporary, dataset = self.load(concepts, [], rules)
        self.addCleanup(temporary.cleanup)
        self.assertEqual("ambiguous", dataset.states[0]["concept_match_status"])
        self.assertEqual([], dataset.concept_bindings)

    def test_concept_relation_endpoints_must_exist(self):
        concepts = [{"concept_id": "c1", "canonical_name": "子类"}]
        relations = [{
            "relation_id": "rel1", "type": "IS_A",
            "child_concept_id": "c1", "parent_concept_id": "missing",
        }]
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        rule_dir, concept_dir = root / "rules", root / "concepts"
        rule_dir.mkdir(); concept_dir.mkdir()
        write_jsonl(rule_dir / "structured_rules.jsonl", [])
        write_jsonl(concept_dir / "concepts.jsonl", concepts)
        write_jsonl(concept_dir / "concept_relations.jsonl", relations)
        with self.assertRaises(GraphImportError):
            load_graph_dataset(rule_dir, concept_dir, "test")

    def test_transition_output_is_not_imported_as_a_graph_node(self):
        rules = [{
            "id": "r1", "conditions": [],
            "antecedents": [{"id": "s1", "object": "电流", "raw_state": "低", "normalized_state": "低"}],
            "consequents": [{"id": "s2", "object": "电流", "raw_state": "高", "normalized_state": "高"}],
            "transitions": [{"object": "电流", "from_state_id": "s1", "to_state_id": "s2"}],
        }]
        temporary, dataset = self.load([], [], rules)
        self.addCleanup(temporary.cleanup)
        self.assertNotIn("transitions", dataset.counts())
        self.assertEqual({"s1", "s2"}, {row["state_id"] for row in dataset.states})


if __name__ == "__main__":
    unittest.main()
