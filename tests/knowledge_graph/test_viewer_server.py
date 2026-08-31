from __future__ import annotations

from http import HTTPStatus
import json
from pathlib import Path
import tempfile
import unittest

from constella.knowledge_graph.viewer_server import (
    FileKnowledgeGraphData,
    KnowledgeGraphData,
    _deduplicate_edges,
    _node,
    make_knowledge_graph_handler,
)


class FakeGraphData:
    def summary(self):
        return {"dataset_id": "test", "nodes": {"Concept": 1}}

    def overview(self, limit):
        return {"title": "overview", "limit": limit, "nodes": [], "edges": []}

    def hierarchy(self, limit):
        return {"title": "hierarchy", "limit": limit, "nodes": [], "edges": []}

    def search(self, query, kind, limit):
        return {"query": query, "kind": kind, "limit": limit, "results": []}

    def entity(self, kind, graph_id):
        if graph_id == "missing":
            raise KeyError(graph_id)
        return {"entity": {"kind": kind, "id": graph_id}, "nodes": [], "edges": []}


class KnowledgeGraphViewerTests(unittest.TestCase):
    def test_file_data_prefers_registered_lifecycle_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registered = {"concept_id": "approved", "canonical_name": "已入库概念"}
            candidate = {"concept_id": "candidate", "canonical_name": "候选概念"}
            (root / "registered_concepts.jsonl").write_text(
                json.dumps(registered, ensure_ascii=False), encoding="utf-8",
            )
            (root / "registered_relations.jsonl").write_text("", encoding="utf-8")
            (root / "concepts.jsonl").write_text(
                json.dumps(candidate, ensure_ascii=False), encoding="utf-8",
            )

            data = FileKnowledgeGraphData(root, "test")

            self.assertEqual(["approved"], list(data.concepts))

    def test_file_data_serves_final_alignment_outputs_without_neo4j(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            concepts = [
                {"concept_id": "parent", "canonical_name": "焊接材料", "aliases": []},
                {"concept_id": "child", "canonical_name": "焊丝", "aliases": ["焊线"]},
                {"concept_id": "isolated", "canonical_name": "孤立概念", "aliases": []},
            ]
            relations = [{
                "relation_id": "r1", "child_concept_id": "child",
                "type": "IS_A", "parent_concept_id": "parent",
            }]
            (root / "final_concepts.jsonl").write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in concepts), encoding="utf-8",
            )
            (root / "final_concept_relations.jsonl").write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in relations), encoding="utf-8",
            )

            data = FileKnowledgeGraphData(root, "test")
            hierarchy = data.hierarchy()

            self.assertEqual(3, data.summary()["nodes"]["Concept"])
            self.assertEqual(["test:parent"], hierarchy["root_ids"])
            self.assertEqual(1, hierarchy["stats"]["isolated_concepts"])
            self.assertEqual("焊丝", data.search("焊线", "concept")["results"][0]["title"])
            self.assertEqual(1, data.entity("concept", "test:child")["stats"]["concept_neighbors"])

    def test_node_labels_keep_composite_state_expression(self):
        node = _node("state", {
            "graph_id": "d:s1", "object": "焊接电流", "normalized_state": "增大",
            "concept_match_status": "unmatched",
        })
        self.assertEqual("焊接电流｜增大", node["title"])
        self.assertEqual("unmatched", node["subtitle"])

    def test_rule_state_edge_direction_matches_graph_schema(self):
        antecedent = KnowledgeGraphData._rule_state_edge("rule", "state", "ANTECEDENT_OF", 0)
        consequent = KnowledgeGraphData._rule_state_edge("rule", "state", "HAS_CONSEQUENT", 1)
        self.assertEqual(("state", "rule"), (antecedent["source"], antecedent["target"]))
        self.assertEqual(("rule", "state"), (consequent["source"], consequent["target"]))

    def test_transition_node_has_viewer_identity(self):
        node = _node("transition", {"graph_id": "d:t1", "transition_id": "t1", "object": "气体粒子"})
        self.assertEqual("transition", node["kind"])
        self.assertEqual("气体粒子", node["title"])

    def test_edges_are_deduplicated_by_stable_id(self):
        edge = {"id": "e", "source": "a", "target": "b", "type": "IS_A", "properties": {}}
        self.assertEqual([edge], _deduplicate_edges([edge, edge]))

    def test_hierarchy_returns_roots_and_excludes_isolated_nodes_from_payload(self):
        data = KnowledgeGraphData.__new__(KnowledgeGraphData)
        data.dataset_id = "test"
        responses = iter([
            [{"concept_count": 4, "relation_count": 2}],
            [
                {
                    "child": {"graph_id": "child", "canonical_name": "焊丝"},
                    "parent": {"graph_id": "root", "canonical_name": "焊接材料"},
                    "type": "IS_A", "properties": {"graph_id": "r1"},
                },
                {
                    "child": {"graph_id": "part", "canonical_name": "药皮"},
                    "parent": {"graph_id": "child", "canonical_name": "焊丝"},
                    "type": "PART_OF", "properties": {"graph_id": "r2"},
                },
            ],
        ])
        data._query = lambda *args, **kwargs: next(responses)

        payload = data.hierarchy()

        self.assertEqual(["root"], payload["root_ids"])
        self.assertEqual(3, payload["stats"]["related_concepts"])
        self.assertEqual(1, payload["stats"]["isolated_concepts"])
        self.assertEqual({"IS_A", "PART_OF"}, {edge["type"] for edge in payload["edges"]})
        self.assertFalse(payload["truncated"])

    def test_hierarchy_exposes_a_cyclic_component_through_a_fallback_root(self):
        data = KnowledgeGraphData.__new__(KnowledgeGraphData)
        data.dataset_id = "test"
        responses = iter([
            [{"concept_count": 2, "relation_count": 2}],
            [
                {
                    "child": {"graph_id": "b", "canonical_name": "B"},
                    "parent": {"graph_id": "a", "canonical_name": "A"},
                    "type": "IS_A", "properties": {},
                },
                {
                    "child": {"graph_id": "a", "canonical_name": "A"},
                    "parent": {"graph_id": "b", "canonical_name": "B"},
                    "type": "IS_A", "properties": {},
                },
            ],
        ])
        data._query = lambda *args, **kwargs: next(responses)

        payload = data.hierarchy()

        self.assertEqual(["a"], payload["root_ids"])
        self.assertEqual(1, payload["stats"]["roots"])

    def test_static_and_api_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            web_dir = Path(directory)
            (web_dir / "index.html").write_text("viewer", encoding="utf-8")
            (web_dir / "app.js").write_text("", encoding="utf-8")
            (web_dir / "styles.css").write_text("", encoding="utf-8")
            handler_type = make_knowledge_graph_handler(web_dir, FakeGraphData())
            handler = handler_type.__new__(handler_type)
            captured = {}
            handler._json = lambda value: captured.update(json=value)
            handler._file = lambda path: captured.update(file=path)
            handler.send_error = lambda status, message: captured.update(error=(status, message))

            handler.path = "/api/summary"
            handler.do_GET()
            self.assertEqual("test", captured.pop("json")["dataset_id"])

            handler.path = "/api/search?q=焊接&kind=concept&limit=7"
            handler.do_GET()
            self.assertEqual({"query": "焊接", "kind": "concept", "limit": 7, "results": []}, captured.pop("json"))

            handler.path = "/api/graph/hierarchy?limit=321"
            handler.do_GET()
            self.assertEqual(321, captured.pop("json")["limit"])

            handler.path = "/api/entity/concept/missing"
            handler.do_GET()
            self.assertEqual(HTTPStatus.NOT_FOUND, captured.pop("error")[0])


if __name__ == "__main__":
    unittest.main()
