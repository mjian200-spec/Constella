from __future__ import annotations

import json

from constella.semantic_alignment.auto_promotion import (
    ConceptAdmissionGate,
    build_concept_admission_candidates,
)
from constella.semantic_alignment.registry import MemorySnapshot


def _memory() -> MemorySnapshot:
    return MemorySnapshot.build([
        {
            "concept_id": "arc", "canonical_name": "电弧", "aliases": [],
            "definition": "两个电极间的持续气体放电", "evidence_ids": ["u1"],
            "source_package_ids": ["p1"],
        },
        {
            "concept_id": "number", "canonical_name": "60", "aliases": [],
            "definition": None, "evidence_ids": ["u2"], "source_package_ids": ["p2"],
        },
    ], [])


def test_type_candidates_collapse_dimensions_and_keep_type_conflict():
    proposals = [
        {
            "proposal_kind": "TYPE_REVIEW", "concept_id": "arc", "concept_type": "object",
            "canonical_name": "电弧", "support": 20, "raw_expressions": ["电弧"],
            "subject_dimension_key": "电弧", "source_state_ids": ["s1"],
            "source_rule_ids": ["r1"], "context_package_ids": ["p1"], "unlock_count": 8,
        },
        {
            "proposal_kind": "TYPE_REVIEW", "concept_id": "arc", "concept_type": "object",
            "canonical_name": "电弧", "support": 8, "raw_expressions": ["焊接电弧"],
            "subject_dimension_key": "焊接电弧", "source_state_ids": ["s2"],
            "source_rule_ids": ["r2"], "context_package_ids": ["p2"], "unlock_count": 8,
        },
        {
            "proposal_kind": "TYPE_REVIEW", "concept_id": "arc", "concept_type": "state",
            "canonical_name": "电弧", "support": 6, "raw_expressions": ["电弧"],
            "subject_dimension_key": "焊接对象", "source_state_ids": ["s3"],
            "source_rule_ids": ["r3"], "context_package_ids": ["p3"], "unlock_count": 8,
        },
        {
            "proposal_kind": "TYPE_REVIEW", "concept_id": "number", "concept_type": "state",
            "canonical_name": "60", "support": 30, "raw_expressions": ["60"],
            "source_state_ids": ["s4"], "source_rule_ids": ["r4"],
            "context_package_ids": ["p4"], "unlock_count": 1,
        },
    ]
    rows = build_concept_admission_candidates(proposals, _memory(), min_support=5)
    assert len(rows) == 1
    assert rows[0]["concept_id"] == "arc"
    assert rows[0]["suggested_types"] == ["object", "state"]
    assert rows[0]["support_by_type"] == {"object": 20, "state": 6}
    assert rows[0]["source_state_count"] == 3


class _FakeClient:
    def complete(self, _model_key, messages, **_kwargs):
        package = json.loads(messages[1]["content"])
        decisions = []
        for candidate in package["candidates"]:
            decisions.append({
                "concept_id": candidate["concept_id"],
                "decision": "APPROVE",
                "selected_type": "object",
                "confidence": "HIGH",
                "boundary_checks": {
                    "stable_kind": True,
                    "not_instance_or_parameter": True,
                    "single_identity": True,
                    "evidence_sufficient": True,
                    "type_clear": True,
                },
                "reason": "定义和多处用法均表示稳定物理现象。",
            })
        return {
            "model": "fake-model",
            "choices": [{"message": {"content": json.dumps({"decisions": decisions})}}],
        }


def test_model_gate_emits_traceable_memory_event(tmp_path):
    candidate = {
        "concept_id": "arc", "canonical_name": "电弧", "aliases": [],
        "definition": "两个电极间的持续气体放电", "definition_type": "explicit",
        "concept_evidence_ids": ["u1"], "concept_source_package_ids": ["p1"],
        "suggested_types": ["object", "state"],
        "support_by_type": {"object": 20, "state": 6},
        "raw_expressions_by_type": {"object": ["电弧"], "state": ["电弧"]},
        "dimensions_by_type": {"object": ["电弧"], "state": ["焊接对象"]},
        "source_state_count": 3, "source_rule_count": 3, "context_package_count": 3,
        "source_state_ids": ["s1", "s2", "s3"], "context_package_ids": ["p1"],
        "unlock_count": 8,
        "deterministic_checks": {
            "known_concept": True, "has_article_evidence": True,
            "minimum_support": True, "not_numeric_expression": True,
        },
    }
    gate = ConceptAdmissionGate(
        {"model": {"model": "fake-model"}}, "model",
        "prompts/semantic_alignment/concept_type_gate_v1.yaml", tmp_path,
        client=_FakeClient(),
    )
    reviews, events, report = gate.run([candidate], memory_version="memory_0")
    assert report["approved_count"] == 1
    assert reviews[0]["gate_status"] == "APPROVED"
    assert events[0]["approval_mode"] == "MODEL_GATE"
    assert events[0]["type"] == "object"
    promoted = MemorySnapshot.build(_memory().concepts, [], events)
    assert next(row for row in promoted.concepts if row["concept_id"] == "arc")["type"] == "object"


def test_post_gate_rejects_medium_confidence():
    candidate = {"suggested_types": ["object"]}
    decision = {
        "decision": "APPROVE", "selected_type": "object", "confidence": "MEDIUM",
        "boundary_checks": {key: True for key in (
            "stable_kind", "not_instance_or_parameter", "single_identity",
            "evidence_sufficient", "type_clear",
        )},
    }
    approved, reason = ConceptAdmissionGate._approval_gate(candidate, decision)
    assert not approved
    assert reason == "approval_requires_high_confidence"
