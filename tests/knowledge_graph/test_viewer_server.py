from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
import tempfile
import unittest

from constella.knowledge_graph.viewer_server import (
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

    def search(self, query, kind, limit):
        return {"query": query, "kind": kind, "limit": limit, "results": []}

    def entity(self, kind, graph_id):
        if graph_id == "missing":
            raise KeyError(graph_id)
        return {"entity": {"kind": kind, "id": graph_id}, "nodes": [], "edges": []}


class KnowledgeGraphViewerTests(unittest.TestCase):
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

            handler.path = "/api/entity/concept/missing"
            handler.do_GET()
            self.assertEqual(HTTPStatus.NOT_FOUND, captured.pop("error")[0])


if __name__ == "__main__":
    unittest.main()
