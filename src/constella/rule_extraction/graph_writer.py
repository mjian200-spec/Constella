from __future__ import annotations

import hashlib
import json
from typing import Any

from neo4j import GraphDatabase

from .models import StateExpression, StructuredRuleSet


class GraphWriteError(RuntimeError):
    pass


def _hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


class Neo4jRuleGraphWriter:
    def __init__(self, uri: str, username: str, password: str, database: str = "neo4j") -> None:
        self.driver = GraphDatabase.driver(uri, auth=(username, password))
        self.database = database

    def close(self) -> None:
        self.driver.close()

    def verify(self) -> None:
        self.driver.verify_connectivity()

    def reset_and_initialize(self, run_id: str, input_fingerprint: str) -> None:
        with self.driver.session(database=self.database) as session:
            # Neo4j forbids schema modifications after data writes in the same
            # transaction. These implicit transactions deliberately remain separate.
            session.run("MATCH (n) DETACH DELETE n").consume()
            session.run("CREATE CONSTRAINT state_identity IF NOT EXISTS FOR (n:StateExpression) REQUIRE n.identity_hash IS UNIQUE").consume()
            session.run("CREATE CONSTRAINT rule_identity IF NOT EXISTS FOR (n:Rule) REQUIRE n.identity_hash IS UNIQUE").consume()
            session.run("CREATE CONSTRAINT run_identity IF NOT EXISTS FOR (n:ExtractionRun) REQUIRE n.run_id IS UNIQUE").consume()
            session.execute_write(self._create_run, run_id, input_fingerprint)

    @staticmethod
    def _create_run(tx, run_id: str, input_fingerprint: str) -> None:
        tx.run(
            "CREATE (:ExtractionRun {run_id:$run_id,input_fingerprint:$fingerprint,status:'running',started_at:datetime()})",
            run_id=run_id, fingerprint=input_fingerprint,
        ).consume()

    def write_ruleset(self, ruleset: StructuredRuleSet, run_id: str) -> None:
        try:
            with self.driver.session(database=self.database) as session:
                session.execute_write(self._write_ruleset, ruleset, run_id)
        except Exception as error:
            raise GraphWriteError(str(error)) from error

    @staticmethod
    def _state_properties(state: StateExpression) -> dict[str, str]:
        identity = {"label": "StateExpression", "object": state.object, "raw_state": state.raw_state, "normalized_state": state.normalized_state}
        return {**identity, "identity_hash": _hash(identity), "id": state.id}

    @classmethod
    def _write_ruleset(cls, tx, ruleset: StructuredRuleSet, run_id: str) -> None:
        run = tx.run("MATCH (r:ExtractionRun {run_id:$run_id}) RETURN r", run_id=run_id).single()
        if not run:
            raise GraphWriteError(f"ExtractionRun {run_id} is missing")
        for rule in ruleset.rules:
            identity = {
                "label": "Rule", "context_package_id": rule.context_package_id, "rule_group_id": rule.rule_group_id,
                "rule_index": rule.rule_index, "relation": rule.relation, "raw_expression": rule.raw_expression,
            }
            props = {**identity, "identity_hash": _hash(identity), "id": rule.id, "run_id": run_id}
            tx.run("MERGE (r:Rule {identity_hash:$props.identity_hash}) SET r += $props", props=props).consume()
            tx.run("MATCH (r:Rule {identity_hash:$hash}),(run:ExtractionRun {run_id:$run_id}) MERGE (r)-[:EXTRACTED_IN]->(run)", hash=props["identity_hash"], run_id=run_id).consume()
            cls._write_side(tx, rule.conditions, "CONDITION", props["identity_hash"])
            cls._write_side(tx, rule.antecedents, "ANTECEDENT", props["identity_hash"])
            cls._write_side(tx, rule.consequents, "CONSEQUENT", props["identity_hash"], outgoing=True)

    @classmethod
    def _write_side(cls, tx, states: list[StateExpression], relation: str, rule_hash: str, *, outgoing: bool = False) -> None:
        for position, state in enumerate(states):
            props = cls._state_properties(state)
            tx.run("MERGE (s:StateExpression {identity_hash:$props.identity_hash}) SET s += $props", props=props).consume()
            if outgoing:
                query = f"MATCH (s:StateExpression {{identity_hash:$state_hash}}),(r:Rule {{identity_hash:$rule_hash}}) MERGE (r)-[e:{relation} {{position:$position}}]->(s)"
            else:
                query = f"MATCH (s:StateExpression {{identity_hash:$state_hash}}),(r:Rule {{identity_hash:$rule_hash}}) MERGE (s)-[e:{relation} {{position:$position}}]->(r)"
            tx.run(query, state_hash=props["identity_hash"], rule_hash=rule_hash, position=position).consume()

    def package_rule_ids(self, package_id: str, run_id: str) -> list[str]:
        with self.driver.session(database=self.database) as session:
            records = session.run(
                "MATCH (r:Rule {context_package_id:$package_id,run_id:$run_id}) RETURN r.id AS id ORDER BY id",
                package_id=package_id, run_id=run_id,
            )
            return [record["id"] for record in records]

    def finish_run(self, run_id: str) -> None:
        with self.driver.session(database=self.database) as session:
            session.run("MATCH (r:ExtractionRun {run_id:$run_id}) SET r.status='completed',r.completed_at=datetime()", run_id=run_id).consume()
