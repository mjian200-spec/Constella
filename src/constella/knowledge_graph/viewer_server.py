"""Read-only Neo4j API and static server for the Knowledge Graph Viewer."""

from __future__ import annotations

import json
import mimetypes
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Type
from urllib.parse import parse_qs, unquote, urlparse

from neo4j import GraphDatabase

from constella.semantic_alignment.models import AlignmentStatus


ENTITY_LABELS = {
    "concept": "Concept",
    "rule": "Rule",
    "state": "StateExpression",
    "transition": "StateTransition",
}


def _node(kind: str, props: dict[str, Any], **extra: Any) -> dict[str, Any]:
    if kind == "concept":
        title = props.get("canonical_name") or props.get("concept_id")
        subtitle = props.get("definition") or "概念"
    elif kind == "rule":
        title = props.get("relation") or "未命名规则"
        subtitle = props.get("raw_expression") or props.get("rule_id")
    elif kind == "transition":
        title = props.get("object") or "状态迁移"
        subtitle = "状态迁移"
    else:
        title = f"{props.get('object') or '未命名对象'}｜{props.get('normalized_state') or props.get('raw_state') or '未描述状态'}"
        subtitle = props.get("concept_match_status") or "状态表达式"
    return {
        "id": props.get("graph_id"), "kind": kind, "title": str(title or ""),
        "subtitle": str(subtitle or ""), "properties": props, **extra,
    }


def _edge(
    edge_id: str, source: str, target: str, relation_type: str,
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": edge_id, "source": source, "target": target,
        "type": relation_type, "properties": properties or {},
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ValueError(f"Missing viewer input: {path}")
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"Invalid JSONL at {path}:{line_number}") from error
            if not isinstance(value, dict):
                raise ValueError(f"Expected an object at {path}:{line_number}")
            rows.append(value)
    return rows


def _hierarchy_payload(
    dataset_id: str,
    relation_rows: list[dict[str, Any]],
    concept_count: int,
    relation_count: int,
) -> dict[str, Any]:
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    child_ids: set[str] = set()
    for row in relation_rows:
        child = _node("concept", row["child"])
        parent = _node("concept", row["parent"])
        nodes[child["id"]] = child
        nodes[parent["id"]] = parent
        child_ids.add(child["id"])
        properties = row["properties"] or {}
        edges.append(_edge(
            str(properties.get("graph_id") or f"{child['id']}:{row['type']}:{parent['id']}"),
            child["id"], parent["id"], row["type"], properties,
        ))
    root_ids = sorted(
        (node_id for node_id in nodes if node_id not in child_ids),
        key=lambda node_id: (nodes[node_id]["title"], node_id),
    )
    children_by_parent: dict[str, set[str]] = {}
    for edge in edges:
        children_by_parent.setdefault(edge["target"], set()).add(edge["source"])
    reachable: set[str] = set()
    pending = list(root_ids)
    while pending:
        current = pending.pop()
        if current in reachable:
            continue
        reachable.add(current)
        pending.extend(children_by_parent.get(current, set()))
    # A malformed cyclic component has no natural root. Add one deterministic
    # entry per unreachable component so the client can expose the bad data.
    while len(reachable) < len(nodes):
        fallback = min(
            (node_id for node_id in nodes if node_id not in reachable),
            key=lambda node_id: (nodes[node_id]["title"], node_id),
        )
        root_ids.append(fallback)
        pending = [fallback]
        while pending:
            current = pending.pop()
            if current in reachable:
                continue
            reachable.add(current)
            pending.extend(children_by_parent.get(current, set()))
    truncated = relation_count > len(relation_rows)
    return {
        "dataset_id": dataset_id,
        "title": "概念层级树",
        "nodes": sorted(nodes.values(), key=lambda node: (node["title"], node["id"])),
        "edges": _deduplicate_edges(edges),
        "root_ids": root_ids,
        "stats": {
            "concepts": concept_count,
            "related_concepts": len(nodes),
            # With a truncated relation window the edges outside it are unknown,
            # so isolated counts would mislabel connected concepts.
            "isolated_concepts": None if truncated else max(0, concept_count - len(nodes)),
            "relations": relation_count,
            "roots": len(root_ids),
        },
        "truncated": truncated,
    }


class KnowledgeGraphData:
    def __init__(
        self, uri: str, username: str, password: str, database: str = "neo4j",
        dataset_id: str = "gmaw_full_20260829", driver=None,
    ) -> None:
        self.driver = driver or GraphDatabase.driver(uri, auth=(username, password))
        self.database = database
        self.dataset_id = dataset_id

    def close(self) -> None:
        self.driver.close()

    def verify(self) -> None:
        self.driver.verify_connectivity()

    def _query(self, cypher: str, **parameters: Any) -> list[dict[str, Any]]:
        parameters.setdefault("dataset_id", self.dataset_id)
        with self.driver.session(database=self.database) as session:
            return [dict(record) for record in session.run(cypher, parameters)]

    def summary(self) -> dict[str, Any]:
        node_rows = self._query("""
            MATCH (n {dataset_id:$dataset_id})
            UNWIND labels(n) AS label
            WITH label, count(n) AS count
            WHERE label IN ['Rule','StateExpression','StateTransition','Concept']
            RETURN label, count ORDER BY label
        """)
        relation_rows = self._query("""
            MATCH (a {dataset_id:$dataset_id})-[r]->(b {dataset_id:$dataset_id})
            RETURN type(r) AS type, count(r) AS count ORDER BY type
        """)
        match_rows = self._query("""
            MATCH (s:StateExpression {dataset_id:$dataset_id})
            RETURN s.concept_match_status AS status, count(s) AS count ORDER BY status
        """)
        unmatched = self._query("""
            MATCH (s:StateExpression {dataset_id:$dataset_id, concept_match_status:'unmatched'})
            RETURN s.object AS object, count(s) AS count
            ORDER BY count DESC, object LIMIT 10
        """)
        return {
            "dataset_id": self.dataset_id,
            "nodes": {str(row["label"]): int(row["count"]) for row in node_rows},
            "relations": {str(row["type"]): int(row["count"]) for row in relation_rows},
            "concept_matches": {str(row["status"]): int(row["count"]) for row in match_rows},
            "top_unmatched_objects": unmatched,
        }

    def overview(self, limit: int = 100) -> dict[str, Any]:
        limit = max(10, min(limit, 180))
        rows = self._query("""
            MATCH (c:Concept {dataset_id:$dataset_id})
            OPTIONAL MATCH (c)-[relation:IS_A|PART_OF]-(:Concept {dataset_id:$dataset_id})
            WITH c, count(relation) AS degree
            ORDER BY degree DESC, c.canonical_name
            LIMIT $limit
            RETURN properties(c) AS entity, degree
        """, limit=limit)
        nodes = [_node("concept", row["entity"], degree=int(row["degree"])) for row in rows]
        graph_ids = [row["id"] for row in nodes]
        relation_rows = self._query("""
            MATCH (child:Concept)-[relation:IS_A|PART_OF]->(parent:Concept)
            WHERE child.graph_id IN $graph_ids AND parent.graph_id IN $graph_ids
            RETURN child.graph_id AS source, parent.graph_id AS target,
                   type(relation) AS type, properties(relation) AS properties
        """, graph_ids=graph_ids)
        edges = [
            _edge(
                str(row["properties"].get("graph_id") or f"{row['source']}:{row['type']}:{row['target']}"),
                row["source"], row["target"], row["type"], row["properties"],
            )
            for row in relation_rows
        ]
        return {"dataset_id": self.dataset_id, "title": "概念结构总览", "nodes": nodes, "edges": edges}

    def hierarchy(self, limit: int = 5000) -> dict[str, Any]:
        """Return the concept hierarchy without flooding the client with isolated concepts."""
        limit = max(1, min(limit, 10000))
        count_rows = self._query("""
            MATCH (concept:Concept {dataset_id:$dataset_id})
            WITH count(concept) AS concept_count
            OPTIONAL MATCH (:Concept {dataset_id:$dataset_id})-[relation:IS_A|PART_OF]->
                           (:Concept {dataset_id:$dataset_id})
            RETURN concept_count, count(relation) AS relation_count
        """)
        counts = count_rows[0] if count_rows else {"concept_count": 0, "relation_count": 0}
        relation_rows = self._query("""
            MATCH (child:Concept {dataset_id:$dataset_id})-[relation:IS_A|PART_OF]->
                  (parent:Concept {dataset_id:$dataset_id})
            RETURN properties(child) AS child, properties(parent) AS parent,
                   type(relation) AS type, properties(relation) AS properties
            ORDER BY parent.canonical_name, child.canonical_name, type(relation)
            LIMIT $limit
        """, limit=limit)
        concept_count = int(counts.get("concept_count") or 0)
        relation_count = int(counts.get("relation_count") or 0)
        return _hierarchy_payload(
            self.dataset_id, relation_rows, concept_count, relation_count,
        )

    def search(self, query: str, kind: str = "all", limit: int = 60) -> dict[str, Any]:
        query = query.strip().lower()
        limit = max(1, min(limit, 100))
        results: list[dict[str, Any]] = []
        if kind in {"all", "concept"}:
            rows = self._query("""
                MATCH (n:Concept {dataset_id:$dataset_id})
                WHERE $query = '' OR toLower(n.canonical_name) CONTAINS $query
                   OR any(alias IN coalesce(n.aliases, []) WHERE toLower(alias) CONTAINS $query)
                   OR toLower(coalesce(n.definition, '')) CONTAINS $query
                RETURN properties(n) AS entity ORDER BY n.canonical_name LIMIT $limit
            """, query=query, limit=limit)
            results.extend(_node("concept", row["entity"]) for row in rows)
        if kind in {"all", "rule"}:
            rows = self._query("""
                MATCH (n:Rule {dataset_id:$dataset_id})
                WHERE $query = '' OR toLower(n.rule_id) CONTAINS $query
                   OR toLower(coalesce(n.relation, '')) CONTAINS $query
                   OR toLower(n.raw_expression) CONTAINS $query
                RETURN properties(n) AS entity ORDER BY n.rule_id LIMIT $limit
            """, query=query, limit=limit)
            results.extend(_node("rule", row["entity"]) for row in rows)
        if kind in {"all", "state", "unmatched"}:
            status = "unmatched" if kind == "unmatched" else None
            rows = self._query("""
                MATCH (n:StateExpression {dataset_id:$dataset_id})
                WHERE ($status IS NULL OR n.concept_match_status = $status)
                  AND ($query = '' OR toLower(n.state_id) CONTAINS $query
                   OR toLower(n.object) CONTAINS $query
                   OR toLower(n.normalized_state) CONTAINS $query)
                RETURN properties(n) AS entity
                ORDER BY n.object, n.normalized_state LIMIT $limit
            """, query=query, status=status, limit=limit)
            results.extend(_node("state", row["entity"]) for row in rows)
        order = {"concept": 0, "rule": 1, "state": 2}
        results.sort(key=lambda row: (order[row["kind"]], row["title"], row["id"]))
        return {"query": query, "kind": kind, "results": results[:limit]}

    def entity(self, kind: str, graph_id: str) -> dict[str, Any]:
        if kind not in ENTITY_LABELS:
            raise KeyError(kind)
        label = ENTITY_LABELS[kind]
        rows = self._query(
            f"MATCH (n:{label} {{graph_id:$graph_id, dataset_id:$dataset_id}}) RETURN properties(n) AS entity",
            graph_id=graph_id,
        )
        if not rows:
            raise KeyError(graph_id)
        entity = rows[0]["entity"]
        if kind == "concept":
            return self._concept_detail(entity)
        if kind == "rule":
            return self._rule_detail(entity)
        if kind == "state":
            return self._state_detail(entity)
        return self._transition_detail(entity)

    def _concept_detail(self, entity: dict[str, Any]) -> dict[str, Any]:
        graph_id = entity["graph_id"]
        relation_rows = self._query("""
            MATCH (concept:Concept {graph_id:$graph_id})-[relation:IS_A|PART_OF]-(neighbor:Concept)
            RETURN properties(neighbor) AS neighbor, properties(relation) AS relation,
                   type(relation) AS type, startNode(relation).graph_id = concept.graph_id AS outgoing
            ORDER BY type, neighbor.canonical_name
        """, graph_id=graph_id)
        usage_rows = self._query("""
            MATCH (state:StateExpression)-[:ABOUT_CONCEPT]->(concept:Concept {graph_id:$graph_id})
            OPTIONAL MATCH (state)-[role:CONDITION_OF|ANTECEDENT_OF|HAS_CONSEQUENT]-(rule:Rule {dataset_id:$dataset_id})
            RETURN properties(state) AS state, properties(rule) AS rule,
                   type(role) AS role, role.position AS position
            ORDER BY state.object, state.normalized_state, rule.rule_id
            LIMIT 120
        """, graph_id=graph_id)
        nodes = {_node("concept", entity)["id"]: _node("concept", entity, focus=True)}
        edges: list[dict[str, Any]] = []
        for row in relation_rows:
            neighbor = _node("concept", row["neighbor"])
            nodes[neighbor["id"]] = neighbor
            source, target = (graph_id, neighbor["id"]) if row["outgoing"] else (neighbor["id"], graph_id)
            props = row["relation"]
            edges.append(_edge(str(props.get("graph_id") or f"{source}:{row['type']}:{target}"), source, target, row["type"], props))
        for row in usage_rows:
            state = _node("state", row["state"])
            nodes[state["id"]] = state
            edges.append(_edge(f"{state['id']}:ABOUT_CONCEPT:{graph_id}", state["id"], graph_id, "ABOUT_CONCEPT"))
            if row.get("rule"):
                rule = _node("rule", row["rule"])
                nodes[rule["id"]] = rule
                edges.append(self._rule_state_edge(rule["id"], state["id"], row["role"], row.get("position")))
        return {
            "entity": _node("concept", entity), "nodes": list(nodes.values()), "edges": _deduplicate_edges(edges),
            "stats": {
                "concept_neighbors": len(relation_rows),
                "state_usages": len({row["state"]["graph_id"] for row in usage_rows}),
                "rule_usages": len({row["rule"]["graph_id"] for row in usage_rows if row.get("rule")}),
            },
        }

    def _rule_detail(self, entity: dict[str, Any]) -> dict[str, Any]:
        graph_id = entity["graph_id"]
        state_rows = self._query("""
            MATCH (rule:Rule {graph_id:$graph_id})-[role:CONDITION_OF|ANTECEDENT_OF|HAS_CONSEQUENT]-(state:StateExpression)
            OPTIONAL MATCH (state)-[:ABOUT_CONCEPT]->(concept:Concept)
            RETURN properties(state) AS state, type(role) AS role, role.position AS position,
                   properties(concept) AS concept
            ORDER BY role, position
        """, graph_id=graph_id)
        transition_rows = self._query("""
            MATCH (rule:Rule {graph_id:$graph_id})-[:HAS_TRANSITION]->(transition:StateTransition)
            MATCH (transition)-[:FROM_STATE]->(from_state:StateExpression)
            MATCH (transition)-[:TO_STATE]->(to_state:StateExpression)
            RETURN properties(transition) AS transition,
                   properties(from_state) AS from_state, properties(to_state) AS to_state
            ORDER BY transition.transition_id
        """, graph_id=graph_id)
        nodes = {graph_id: _node("rule", entity, focus=True)}
        edges: list[dict[str, Any]] = []
        for row in state_rows:
            state = _node("state", row["state"])
            nodes[state["id"]] = state
            edges.append(self._rule_state_edge(graph_id, state["id"], row["role"], row.get("position")))
            if row.get("concept"):
                concept = _node("concept", row["concept"])
                nodes[concept["id"]] = concept
                edges.append(_edge(f"{state['id']}:ABOUT_CONCEPT:{concept['id']}", state["id"], concept["id"], "ABOUT_CONCEPT"))
        for row in transition_rows:
            transition = _node("transition", row["transition"])
            from_state = _node("state", row["from_state"])
            to_state = _node("state", row["to_state"])
            for node in (transition, from_state, to_state):
                nodes[node["id"]] = node
            edges.extend([
                _edge(f"{graph_id}:HAS_TRANSITION:{transition['id']}", graph_id, transition["id"], "HAS_TRANSITION"),
                _edge(f"{transition['id']}:FROM_STATE", transition["id"], from_state["id"], "FROM_STATE"),
                _edge(f"{transition['id']}:TO_STATE", transition["id"], to_state["id"], "TO_STATE"),
            ])
        return {
            "entity": _node("rule", entity), "nodes": list(nodes.values()), "edges": _deduplicate_edges(edges),
            "stats": {"states": len(state_rows), "transitions": len(transition_rows)},
        }

    def _state_detail(self, entity: dict[str, Any]) -> dict[str, Any]:
        graph_id = entity["graph_id"]
        rows = self._query("""
            MATCH (state:StateExpression {graph_id:$graph_id})
            OPTIONAL MATCH (state)-[:ABOUT_CONCEPT]->(concept:Concept)
            OPTIONAL MATCH (state)-[role:CONDITION_OF|ANTECEDENT_OF|HAS_CONSEQUENT]-(rule:Rule {dataset_id:$dataset_id})
            RETURN properties(concept) AS concept, properties(rule) AS rule,
                   type(role) AS role, role.position AS position
            ORDER BY rule.rule_id
            LIMIT 120
        """, graph_id=graph_id)
        nodes = {graph_id: _node("state", entity, focus=True)}
        edges: list[dict[str, Any]] = []
        for row in rows:
            if row.get("concept"):
                concept = _node("concept", row["concept"])
                nodes[concept["id"]] = concept
                edges.append(_edge(f"{graph_id}:ABOUT_CONCEPT:{concept['id']}", graph_id, concept["id"], "ABOUT_CONCEPT"))
            if row.get("rule"):
                rule = _node("rule", row["rule"])
                nodes[rule["id"]] = rule
                edges.append(self._rule_state_edge(rule["id"], graph_id, row["role"], row.get("position")))
        return {
            "entity": _node("state", entity), "nodes": list(nodes.values()), "edges": _deduplicate_edges(edges),
            "stats": {"rules": len({row["rule"]["graph_id"] for row in rows if row.get("rule")})},
        }

    def _transition_detail(self, entity: dict[str, Any]) -> dict[str, Any]:
        graph_id = entity["graph_id"]
        rows = self._query("""
            MATCH (rule:Rule)-[:HAS_TRANSITION]->(transition:StateTransition {graph_id:$graph_id})
            MATCH (transition)-[:FROM_STATE]->(from_state:StateExpression)
            MATCH (transition)-[:TO_STATE]->(to_state:StateExpression)
            RETURN properties(rule) AS rule,
                   properties(from_state) AS from_state, properties(to_state) AS to_state
        """, graph_id=graph_id)
        if not rows:
            raise KeyError(graph_id)
        row = rows[0]
        transition = _node("transition", entity, focus=True)
        rule = _node("rule", row["rule"])
        from_state = _node("state", row["from_state"])
        to_state = _node("state", row["to_state"])
        return {
            "entity": _node("transition", entity),
            "nodes": [transition, rule, from_state, to_state],
            "edges": [
                _edge(f"{rule['id']}:HAS_TRANSITION:{graph_id}", rule["id"], graph_id, "HAS_TRANSITION"),
                _edge(f"{graph_id}:FROM_STATE", graph_id, from_state["id"], "FROM_STATE"),
                _edge(f"{graph_id}:TO_STATE", graph_id, to_state["id"], "TO_STATE"),
            ],
            "stats": {"states": 2, "rules": 1},
        }

    @staticmethod
    def _rule_state_edge(rule_id: str, state_id: str, role: str, position: int | None) -> dict[str, Any]:
        source, target = (rule_id, state_id) if role == "HAS_CONSEQUENT" else (state_id, rule_id)
        return _edge(f"{source}:{role}:{target}:{position}", source, target, role, {"position": position})


class FileKnowledgeGraphData:
    """Read-only viewer backed by JSONL artifacts (no Neo4j).

    Concept inputs are discovered in order: lifecycle registered snapshots,
    final concept-layer outputs, then generic concept-layer outputs. When none
    exist, concepts are rebuilt from alignment candidate payloads.
    """

    def __init__(self, concept_dir: str | Path, dataset_id: str) -> None:
        directory = Path(concept_dir)
        candidates = [
            ("registered_concepts.jsonl", "registered_relations.jsonl"),
            ("final_concepts.jsonl", "final_concept_relations.jsonl"),
            ("concepts.jsonl", "concept_relations.jsonl"),
        ]
        concept_path, relation_path = next(
            (
                (directory / concept_name, directory / relation_name)
                for concept_name, relation_name in candidates
                if (directory / concept_name).is_file()
            ),
            (directory / "concepts.jsonl", directory / "concept_relations.jsonl"),
        )
        self.dataset_id = dataset_id
        if concept_path.is_file():
            raw_concepts = _read_jsonl(concept_path)
            raw_relations = _read_jsonl(relation_path) if relation_path.is_file() else []
        else:
            raw_concepts, raw_relations = self._concepts_from_alignment_outputs(directory)
        self.concepts: dict[str, dict[str, Any]] = {}
        for row in raw_concepts:
            concept_id = str(row.get("concept_id") or "")
            canonical_name = str(row.get("canonical_name") or "")
            if not concept_id or not canonical_name:
                raise ValueError("Every viewer concept requires concept_id and canonical_name")
            if concept_id in self.concepts:
                raise ValueError(f"Duplicate concept_id: {concept_id}")
            self.concepts[concept_id] = {
                **row,
                "graph_id": f"{dataset_id}:{concept_id}",
                "dataset_id": dataset_id,
            }
        self.relations: list[dict[str, Any]] = []
        for row in raw_relations:
            child_id = str(row.get("child_concept_id") or "")
            parent_id = str(row.get("parent_concept_id") or "")
            relation_type = str(row.get("type") or "")
            if relation_type not in {"IS_A", "PART_OF"}:
                continue
            if child_id not in self.concepts or parent_id not in self.concepts:
                raise ValueError(f"Unknown concept relation endpoint: {child_id} -> {parent_id}")
            relation_id = str(row.get("relation_id") or f"{child_id}:{relation_type}:{parent_id}")
            self.relations.append({
                **row,
                "graph_id": f"{dataset_id}:{relation_id}",
                "dataset_id": dataset_id,
                "child_concept_id": child_id,
                "parent_concept_id": parent_id,
                "type": relation_type,
            })
        self.state_rows = self._read_state_rows(directory)

    @staticmethod
    def _concepts_from_alignment_outputs(directory: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        concepts_by_id: dict[str, dict[str, Any]] = {}
        paths = [*sorted(directory.glob("object_semantics*.jsonl")),
                 *sorted(directory.glob("state_semantics*.jsonl"))]
        for path in paths:
            for row in _read_jsonl(path):
                candidates = []
                for core in row.get("core_objects") or []:
                    candidates.extend(core.get("candidates") or [])
                candidates.extend(row.get("state_candidates") or [])
                for candidate in candidates:
                    concept_id = str(candidate.get("id") or "")
                    if not concept_id:
                        continue
                    merged = concepts_by_id.setdefault(concept_id, {"concept_id": concept_id})
                    for key in ("name", "canonical_name", "aliases", "type", "definition",
                                "evidence_ids", "source_package_ids"):
                        if key in candidate and not merged.get(key):
                            merged[key] = candidate[key]
        concepts = []
        for concept_id, row in concepts_by_id.items():
            canonical_name = str(row.pop("name", "") or row.get("canonical_name") or "")
            concepts.append({"concept_id": concept_id, "canonical_name": canonical_name, **row})
        if not concepts:
            names = ", ".join(sorted(path.name for path in directory.iterdir())) if directory.is_dir() else ""
            raise ValueError(
                f"Missing viewer input: {directory} contains no concepts.jsonl and no "
                f"object_semantics*.jsonl/state_semantics*.jsonl outputs. Found: {names or '<empty>'}"
            )
        return concepts, []

    @staticmethod
    def _read_state_rows(directory: Path) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in sorted(directory.glob("state_semantics*.jsonl")):
            rows.extend(_read_jsonl(path))
        return rows

    def close(self) -> None:
        return

    def verify(self) -> None:
        return

    def summary(self) -> dict[str, Any]:
        relation_counts: dict[str, int] = {}
        for relation in self.relations:
            relation_type = relation["type"]
            relation_counts[relation_type] = relation_counts.get(relation_type, 0) + 1
        return {
            "dataset_id": self.dataset_id,
            "nodes": {
                "Concept": len(self.concepts), "Rule": 0,
                "StateExpression": 0, "StateTransition": 0,
            },
            "relations": relation_counts,
            "concept_matches": {"matched": 0, "unmatched": 0},
            "top_unmatched_objects": [],
            "source_mode": "files",
        }

    def _relation_rows(self, limit: int | None = None) -> list[dict[str, Any]]:
        rows = [{
            "child": self.concepts[row["child_concept_id"]],
            "parent": self.concepts[row["parent_concept_id"]],
            "type": row["type"],
            "properties": row,
        } for row in self.relations]
        rows.sort(key=lambda row: (
            str(row["parent"].get("canonical_name") or ""),
            str(row["child"].get("canonical_name") or ""),
            row["type"],
        ))
        return rows if limit is None else rows[:limit]

    def hierarchy(self, limit: int = 5000) -> dict[str, Any]:
        limit = max(1, min(limit, 10000))
        return _hierarchy_payload(
            self.dataset_id, self._relation_rows(limit),
            len(self.concepts), len(self.relations),
        )

    def overview(self, limit: int = 100) -> dict[str, Any]:
        limit = max(10, min(limit, 180))
        degree: dict[str, int] = {}
        for relation in self.relations:
            for concept_id in (relation["child_concept_id"], relation["parent_concept_id"]):
                degree[concept_id] = degree.get(concept_id, 0) + 1
        concept_ids = sorted(
            self.concepts,
            key=lambda concept_id: (
                -degree.get(concept_id, 0),
                str(self.concepts[concept_id].get("canonical_name") or ""),
            ),
        )[:limit]
        graph_ids = {self.concepts[concept_id]["graph_id"] for concept_id in concept_ids}
        nodes = [
            _node("concept", self.concepts[concept_id], degree=degree.get(concept_id, 0))
            for concept_id in concept_ids
        ]
        edges = []
        for row in self._relation_rows():
            source, target = row["child"]["graph_id"], row["parent"]["graph_id"]
            if source in graph_ids and target in graph_ids:
                edges.append(_edge(row["properties"]["graph_id"], source, target, row["type"], row["properties"]))
        return {"dataset_id": self.dataset_id, "title": "概念结构总览", "nodes": nodes, "edges": edges}

    def search(self, query: str, kind: str = "all", limit: int = 60) -> dict[str, Any]:
        query = query.strip().lower()
        limit = max(1, min(limit, 100))
        results: list[dict[str, Any]] = []
        if kind in {"all", "concept"}:
            for props in sorted(self.concepts.values(), key=lambda row: str(row["canonical_name"])):
                values = [props.get("canonical_name"), props.get("definition"), *(props.get("aliases") or [])]
                if not query or any(query in str(value or "").lower() for value in values):
                    results.append(_node("concept", props))
                    if len(results) >= limit:
                        break
        if kind in {"all", "state", "unmatched"}:
            for row in self.state_rows:
                if kind == "unmatched" and row.get("alignment_status") == AlignmentStatus.MATCHED:
                    continue
                values = [row.get("raw_state"), row.get("normalized_input_state"),
                          row.get("canonical_surface"), row.get("raw_object")]
                if query and not any(query in str(value or "").lower() for value in values):
                    continue
                props = {
                    **row,
                    "graph_id": f"{self.dataset_id}:{row.get('source_state_id')}",
                    "state_id": row.get("source_state_id"),
                    "object": row.get("raw_object"),
                    "raw_state": row.get("raw_state"),
                    "normalized_state": row.get("normalized_input_state"),
                    "concept_match_status": (
                        "matched" if row.get("alignment_status") == AlignmentStatus.MATCHED
                        else "unmatched"
                    ),
                }
                results.append(_node("state", props))
                if len(results) >= limit:
                    break
        return {"query": query, "kind": kind, "results": results}

    def entity(self, kind: str, graph_id: str) -> dict[str, Any]:
        if kind != "concept":
            raise KeyError(graph_id)
        entity = next((row for row in self.concepts.values() if row["graph_id"] == graph_id), None)
        if entity is None:
            raise KeyError(graph_id)
        nodes = {graph_id: _node("concept", entity, focus=True)}
        edges: list[dict[str, Any]] = []
        for row in self._relation_rows():
            source, target = row["child"]["graph_id"], row["parent"]["graph_id"]
            if graph_id not in {source, target}:
                continue
            neighbor = row["parent"] if source == graph_id else row["child"]
            nodes[neighbor["graph_id"]] = _node("concept", neighbor)
            edges.append(_edge(row["properties"]["graph_id"], source, target, row["type"], row["properties"]))
        return {
            "entity": _node("concept", entity),
            "nodes": list(nodes.values()), "edges": edges,
            "stats": {"concept_neighbors": len(edges), "state_usages": 0, "rule_usages": 0},
        }


def _deduplicate_edges(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return list({edge["id"]: edge for edge in edges}.values())


def make_knowledge_graph_handler(
    web_dir: Path, data: KnowledgeGraphData | FileKnowledgeGraphData,
) -> Type[SimpleHTTPRequestHandler]:
    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            query = parse_qs(parsed.query)
            try:
                if path in {"/", "/index.html"}:
                    return self._file(web_dir / "index.html")
                if path in {"/app.js", "/styles.css"}:
                    return self._file(web_dir / path.lstrip("/"))
                if path == "/api/summary":
                    return self._json(data.summary())
                if path == "/api/graph/overview":
                    return self._json(data.overview(_integer(query, "limit", 100)))
                if path == "/api/graph/hierarchy":
                    return self._json(data.hierarchy(_integer(query, "limit", 5000)))
                if path == "/api/search":
                    return self._json(data.search(
                        _string(query, "q"), _string(query, "kind", "all"), _integer(query, "limit", 60),
                    ))
                if path.startswith("/api/entity/"):
                    _, _, _, kind, graph_id = path.split("/", 4)
                    return self._json(data.entity(kind, graph_id))
                self.send_error(HTTPStatus.NOT_FOUND, "Unknown graph viewer resource")
            except KeyError:
                self.send_error(HTTPStatus.NOT_FOUND, "Graph entity not found")
            except ValueError as error:
                self.send_error(HTTPStatus.BAD_REQUEST, str(error))
            except (BrokenPipeError, ConnectionResetError):
                # Headless screenshots and closed tabs may leave an in-flight
                # read-only graph query after the client has disconnected.
                return
            except Exception as error:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

        def _json(self, value: Any) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _file(self, path: Path) -> None:
            if not path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND, "Graph viewer resource not found")
                return
            body = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            print(f"[knowledge-graph] {self.address_string()} - {format % args}")

    return Handler


def _string(query: dict[str, list[str]], name: str, default: str = "") -> str:
    return str((query.get(name) or [default])[0])


def _integer(query: dict[str, list[str]], name: str, default: int) -> int:
    try:
        return int(_string(query, name, str(default)))
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error


def serve_knowledge_graph(
    web_dir: str | Path, uri: str, username: str, password: str,
    database: str = "neo4j", dataset_id: str = "gmaw_full_20260829",
    host: str = "127.0.0.1", port: int = 8767,
) -> None:
    data = KnowledgeGraphData(uri, username, password, database, dataset_id)
    data.verify()
    _serve_knowledge_graph(web_dir, data, host, port)


def serve_file_knowledge_graph(
    web_dir: str | Path, concept_dir: str | Path,
    dataset_id: str = "semantic_alignment", host: str = "127.0.0.1", port: int = 8767,
) -> None:
    data = FileKnowledgeGraphData(concept_dir, dataset_id)
    _serve_knowledge_graph(web_dir, data, host, port)


def _serve_knowledge_graph(
    web_dir: str | Path, data: KnowledgeGraphData | FileKnowledgeGraphData,
    host: str, port: int,
) -> None:
    server = ThreadingHTTPServer((host, port), make_knowledge_graph_handler(Path(web_dir), data))
    print(f"Constella Knowledge Graph: http://{host}:{port}/ [{data.dataset_id}]")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        data.close()
