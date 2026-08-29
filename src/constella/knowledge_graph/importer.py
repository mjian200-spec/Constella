from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Iterable, Iterator

from neo4j import GraphDatabase


class GraphImportError(RuntimeError):
    pass


def _read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.is_file():
        raise GraphImportError(f"Missing graph import input: {path}")
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise GraphImportError(f"Invalid JSONL at {path}:{line_number}") from error
            if not isinstance(value, dict):
                raise GraphImportError(f"Expected an object at {path}:{line_number}")
            yield value


def normalize_name(value: str) -> str:
    return "".join(str(value).split()).lower()


def _graph_id(dataset_id: str, entity_id: str) -> str:
    return f"{dataset_id}:{entity_id}"


@dataclass(slots=True)
class GraphDataset:
    dataset_id: str
    rules: list[dict[str, Any]]
    states: list[dict[str, Any]]
    rule_states: list[dict[str, Any]]
    concepts: list[dict[str, Any]]
    concept_relations: list[dict[str, Any]]
    concept_bindings: list[dict[str, Any]]

    def counts(self) -> dict[str, int]:
        return {
            "rules": len(self.rules),
            "states": len(self.states),
            "rule_states": len(self.rule_states),
            "concepts": len(self.concepts),
            "concept_relations": len(self.concept_relations),
            "concept_bindings": len(self.concept_bindings),
            "matched_states": sum(row["concept_match_status"] == "matched" for row in self.states),
            "ambiguous_states": sum(row["concept_match_status"] == "ambiguous" for row in self.states),
            "unmatched_states": sum(row["concept_match_status"] == "unmatched" for row in self.states),
        }


def load_graph_dataset(
    rule_output_dir: str | Path,
    concept_output_dir: str | Path,
    dataset_id: str,
) -> GraphDataset:
    if not dataset_id.strip():
        raise GraphImportError("dataset_id must not be empty")
    rule_path = Path(rule_output_dir) / "structured_rules.jsonl"
    concept_dir = Path(concept_output_dir)
    raw_rules = list(_read_jsonl(rule_path))
    raw_concepts = list(_read_jsonl(concept_dir / "concepts.jsonl"))
    raw_relations = list(_read_jsonl(concept_dir / "concept_relations.jsonl"))

    concepts, concept_names = _prepare_concepts(raw_concepts, dataset_id)
    rules, states, rule_states = _prepare_rules(raw_rules, dataset_id)
    bindings = _prepare_concept_bindings(states, concept_names, dataset_id)
    relations = _prepare_concept_relations(raw_relations, {row["concept_id"] for row in concepts}, dataset_id)
    return GraphDataset(dataset_id, rules, states, rule_states, concepts, relations, bindings)


def _prepare_concepts(
    rows: list[dict[str, Any]], dataset_id: str,
) -> tuple[list[dict[str, Any]], dict[str, list[tuple[str, str]]]]:
    concepts: dict[str, dict[str, Any]] = {}
    names: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        concept_id = str(row.get("concept_id") or "")
        canonical_name = str(row.get("canonical_name") or "")
        if not concept_id or not canonical_name:
            raise GraphImportError("Every concept requires concept_id and canonical_name")
        if concept_id in concepts:
            raise GraphImportError(f"Duplicate concept_id: {concept_id}")
        aliases = [str(value) for value in row.get("aliases") or [] if str(value)]
        concepts[concept_id] = {
            "graph_id": _graph_id(dataset_id, concept_id),
            "dataset_id": dataset_id,
            "concept_id": concept_id,
            "canonical_name": canonical_name,
            "aliases": aliases,
            "definition": row.get("definition"),
            "definition_type": row.get("definition_type"),
            "audit_status": row.get("audit_status"),
            "origin_depth": row.get("origin_depth"),
            "source_package_ids": [str(value) for value in row.get("source_package_ids") or []],
            "evidence_ids": [str(value) for value in row.get("evidence_ids") or []],
        }
        names.setdefault(normalize_name(canonical_name), []).append((concept_id, "exact_canonical"))
        for alias in aliases:
            names.setdefault(normalize_name(alias), []).append((concept_id, "exact_alias"))
    return list(concepts.values()), names


def _prepare_rules(
    rows: list[dict[str, Any]], dataset_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    rules: list[dict[str, Any]] = []
    states: dict[str, dict[str, Any]] = {}
    rule_states: list[dict[str, Any]] = []
    seen_rule_ids: set[str] = set()
    for row in rows:
        rule_id = str(row.get("id") or "")
        if not rule_id:
            raise GraphImportError("Every rule requires id")
        if rule_id in seen_rule_ids:
            raise GraphImportError(f"Duplicate rule id: {rule_id}")
        seen_rule_ids.add(rule_id)
        rules.append({
            "graph_id": _graph_id(dataset_id, rule_id),
            "dataset_id": dataset_id,
            "rule_id": rule_id,
            "context_package_id": str(row.get("context_package_id") or ""),
            "rule_group_id": str(row.get("rule_group_id") or ""),
            "rule_index": int(row.get("rule_index") or 0),
            "relation": row.get("relation"),
            "raw_expression": str(row.get("raw_expression") or ""),
        })
        for side in ("conditions", "antecedents", "consequents"):
            for position, state in enumerate(row.get(side) or []):
                state_id = _add_state(states, state, dataset_id)
                rule_states.append({
                    "graph_id": _graph_id(dataset_id, f"{rule_id}:{side}:{position}"),
                    "rule_graph_id": _graph_id(dataset_id, rule_id),
                    "state_graph_id": _graph_id(dataset_id, state_id),
                    "side": side,
                    "position": position,
                })
    return rules, list(states.values()), rule_states


def _add_state(states: dict[str, dict[str, Any]], row: dict[str, Any], dataset_id: str) -> str:
    state_id = str(row.get("id") or "")
    if not state_id:
        raise GraphImportError("Every state expression requires id")
    value = {
        "graph_id": _graph_id(dataset_id, state_id),
        "dataset_id": dataset_id,
        "state_id": state_id,
        "object": str(row.get("object") or ""),
        "raw_state": str(row.get("raw_state") or ""),
        "normalized_state": str(row.get("normalized_state") or row.get("raw_state") or ""),
        "concept_match_status": "unmatched",
    }
    existing = states.get(state_id)
    if existing is not None and existing != value:
        raise GraphImportError(f"State id {state_id} maps to inconsistent expressions")
    states[state_id] = value
    return state_id


def _prepare_concept_bindings(
    states: list[dict[str, Any]],
    concept_names: dict[str, list[tuple[str, str]]],
    dataset_id: str,
) -> list[dict[str, Any]]:
    bindings: list[dict[str, Any]] = []
    for state in states:
        matches = list(dict.fromkeys(concept_names.get(normalize_name(state["object"]), [])))
        concept_ids = {concept_id for concept_id, _ in matches}
        if len(concept_ids) > 1:
            state["concept_match_status"] = "ambiguous"
            continue
        if not matches:
            continue
        concept_id = matches[0][0]
        method = "exact_canonical" if any(item[1] == "exact_canonical" for item in matches) else "exact_alias"
        state["concept_match_status"] = "matched"
        bindings.append({
            "graph_id": _graph_id(dataset_id, f"{state['state_id']}:{concept_id}"),
            "state_graph_id": state["graph_id"],
            "concept_graph_id": _graph_id(dataset_id, concept_id),
            "match_method": method,
            "confidence": 1.0,
        })
    return bindings


def _prepare_concept_relations(
    rows: list[dict[str, Any]], concept_ids: set[str], dataset_id: str,
) -> list[dict[str, Any]]:
    relations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        relation_id = str(row.get("relation_id") or "")
        relation_type = str(row.get("type") or "")
        child_id = str(row.get("child_concept_id") or "")
        parent_id = str(row.get("parent_concept_id") or "")
        if not relation_id or relation_type not in {"IS_A", "PART_OF"}:
            raise GraphImportError(f"Invalid concept relation: {relation_id or '<missing id>'}")
        if relation_id in seen:
            raise GraphImportError(f"Duplicate concept relation id: {relation_id}")
        if child_id not in concept_ids or parent_id not in concept_ids:
            raise GraphImportError(f"Concept relation {relation_id} has missing endpoints")
        seen.add(relation_id)
        relations.append({
            "graph_id": _graph_id(dataset_id, relation_id),
            "relation_id": relation_id,
            "type": relation_type,
            "child_graph_id": _graph_id(dataset_id, child_id),
            "parent_graph_id": _graph_id(dataset_id, parent_id),
            "directness": row.get("directness"),
            "audit_status": row.get("audit_status"),
            "source_package_ids": [str(value) for value in row.get("source_package_ids") or []],
            "evidence_ids": [str(value) for value in row.get("evidence_ids") or []],
        })
    return relations


def _batches(rows: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for start in range(0, len(rows), size):
        yield rows[start:start + size]


class Neo4jKnowledgeGraphImporter:
    def __init__(self, uri: str, username: str, password: str, database: str = "neo4j", batch_size: int = 1000) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        self.driver = GraphDatabase.driver(uri, auth=(username, password))
        self.database = database
        self.batch_size = batch_size

    def close(self) -> None:
        self.driver.close()

    def verify(self) -> None:
        self.driver.verify_connectivity()

    def import_dataset(self, dataset: GraphDataset) -> dict[str, int]:
        self._create_constraints()
        with self.driver.session(database=self.database) as session:
            self._write_rows(session, dataset.concepts, "MERGE (n:Concept {graph_id: row.graph_id}) SET n += row")
            self._write_concept_relations(session, dataset.concept_relations)
            self._write_rows(session, dataset.rules, "MERGE (n:Rule {graph_id: row.graph_id}) SET n += row")
            self._write_rows(session, dataset.states, "MERGE (n:StateExpression {graph_id: row.graph_id}) SET n += row")
            self._write_rule_states(session, dataset.rule_states)
            self._write_bindings(session, dataset.concept_bindings)
        return self.count_dataset(dataset.dataset_id)

    def _create_constraints(self) -> None:
        constraints = {
            "kg_rule_graph_id": "Rule",
            "kg_state_graph_id": "StateExpression",
            "kg_concept_graph_id": "Concept",
        }
        with self.driver.session(database=self.database) as session:
            for name, label in constraints.items():
                session.run(
                    f"CREATE CONSTRAINT {name} IF NOT EXISTS FOR (n:{label}) REQUIRE n.graph_id IS UNIQUE"
                ).consume()

    def _write_rows(self, session, rows: list[dict[str, Any]], statement: str) -> None:
        for batch in _batches(rows, self.batch_size):
            session.run(f"UNWIND $rows AS row {statement}", rows=batch).consume()

    def _write_concept_relations(self, session, rows: list[dict[str, Any]]) -> None:
        for relation_type in ("IS_A", "PART_OF"):
            selected = [row for row in rows if row["type"] == relation_type]
            query = f"""
                MATCH (child:Concept {{graph_id: row.child_graph_id}})
                MATCH (parent:Concept {{graph_id: row.parent_graph_id}})
                MERGE (child)-[r:{relation_type} {{graph_id: row.graph_id}}]->(parent)
                SET r.relation_id = row.relation_id,
                    r.directness = row.directness,
                    r.audit_status = row.audit_status,
                    r.source_package_ids = row.source_package_ids,
                    r.evidence_ids = row.evidence_ids
            """
            self._write_rows(session, selected, query.strip())

    def _write_rule_states(self, session, rows: list[dict[str, Any]]) -> None:
        relation_types = {
            "conditions": ("CONDITION_OF", "state", "rule"),
            "antecedents": ("ANTECEDENT_OF", "state", "rule"),
            "consequents": ("HAS_CONSEQUENT", "rule", "state"),
        }
        for side, (relation_type, source, target) in relation_types.items():
            selected = [row for row in rows if row["side"] == side]
            query = f"""
                MATCH (state:StateExpression {{graph_id: row.state_graph_id}})
                MATCH (rule:Rule {{graph_id: row.rule_graph_id}})
                MERGE ({source})-[r:{relation_type} {{graph_id: row.graph_id}}]->({target})
                SET r.position = row.position
            """
            self._write_rows(session, selected, query.strip())

    def _write_bindings(self, session, rows: list[dict[str, Any]]) -> None:
        self._write_rows(session, rows, """
            MATCH (state:StateExpression {graph_id: row.state_graph_id})
            MATCH (concept:Concept {graph_id: row.concept_graph_id})
            MERGE (state)-[r:ABOUT_CONCEPT {graph_id: row.graph_id}]->(concept)
            SET r.match_method = row.match_method, r.confidence = row.confidence
        """.strip())

    def count_dataset(self, dataset_id: str) -> dict[str, int]:
        node_labels = ("Rule", "StateExpression", "Concept")
        result: dict[str, int] = {}
        with self.driver.session(database=self.database) as session:
            for label in node_labels:
                record = session.run(
                    f"MATCH (n:{label} {{dataset_id:$dataset_id}}) RETURN count(n) AS count",
                    dataset_id=dataset_id,
                ).single()
                result[label] = int(record["count"] if record else 0)
            record = session.run("""
                MATCH (a {dataset_id:$dataset_id})-[r]->(b {dataset_id:$dataset_id})
                RETURN type(r) AS type, count(r) AS count
                ORDER BY type
            """, dataset_id=dataset_id)
            result.update({str(row["type"]): int(row["count"]) for row in record})
        return result
