from __future__ import annotations

import json

from constella.semantic_alignment.concept_admission import (
    SerialConceptAdmissionRunner,
    build_initial_pending_concepts,
    build_pending_concepts_from_proposals,
    recall_concept_evidence,
)
from constella.semantic_alignment.packages import AlignmentInputs
from constella.semantic_alignment.registry import MemorySnapshot


def _decision(candidate, *, merge_target=None):
    merge = merge_target is not None
    return {
        "concept_id": candidate["concept_id"],
        "decision": "MERGE" if merge else "APPROVE",
        "target_concept_id": merge_target,
        "selected_type": None if merge else "object",
        "canonical_name": None if merge else candidate["canonical_name"],
        "aliases": [],
        "definition": None if merge else candidate.get("definition"),
        "evidence_ids": [row["evidence_id"] for row in candidate.get("evidence") or []],
        "relations": [],
        "missing_relation_concepts": [],
        "confidence": "HIGH",
        "boundary_checks": {
            "stable_kind": True,
            "not_instance_or_parameter": True,
            "single_identity": True,
            "evidence_sufficient": True,
            "type_clear": True,
            "not_duplicate": not merge,
        },
        "reason": "证据支持稳定且边界清晰的概念身份。",
    }


class _SerialFakeClient:
    def __init__(self):
        self.memory_versions = []

    def complete(self, _model_key, messages, **_kwargs):
        package = json.loads(messages[1]["content"])
        self.memory_versions.append(package["memory_version"])
        candidate = package["candidate"]
        target = next((
            row["id"] for row in package["registered_candidates"]
            if candidate["canonical_name"] == "弧光" and row["name"] == "电弧"
        ), None)
        output = _decision(candidate, merge_target=target)
        return {
            "model": "fake-model",
            "choices": [{"message": {"content": json.dumps(output, ensure_ascii=False)}}],
        }


def test_initial_candidates_include_zero_occurrence_extracted_concepts():
    inputs = AlignmentInputs(
        concepts=[
            {"concept_id": "arc", "canonical_name": "电弧", "aliases": [], "evidence_ids": ["u1"]},
            {"concept_id": "rare", "canonical_name": "稀有概念", "aliases": [], "evidence_ids": ["u2"]},
        ],
        relations=[],
        rules=[{
            "id": "r1", "conditions": [],
            "antecedents": [{"id": "s1", "object": "电弧"}], "consequents": [],
        }],
        context_packages={},
        units={
            "u1": {"id": "u1", "type": "text", "content": "电弧是一种气体放电。"},
            "u2": {"id": "u2", "type": "text", "content": "这里定义稀有概念。"},
        },
    )

    rows = build_initial_pending_concepts(inputs, MemorySnapshot.build(inputs.concepts, []))

    assert [row["concept_id"] for row in rows] == ["arc", "rare"]
    assert [row["occurrence_count"] for row in rows] == [1, 0]
    assert rows[1]["evidence"][0]["evidence_id"] == "u2"


def test_book_recall_prefers_explicit_evidence_before_other_name_hits():
    units = {
        "u1": {"id": "u1", "type": "text", "content": "电弧电弧"},
        "u2": {"id": "u2", "type": "text", "content": "电弧的定义"},
    }

    rows = recall_concept_evidence({
        "canonical_name": "电弧", "aliases": [], "evidence_ids": ["u2"],
    }, units)

    assert [row["evidence_id"] for row in rows] == ["u2", "u1"]


def test_alignment_proposals_become_ranked_evidence_bound_candidates():
    inputs = AlignmentInputs(
        concepts=[], relations=[], rules=[],
        context_packages={
            "p1": {"id": "p1", "core_unit_ids": ["u1"], "support_unit_ids": []},
        },
        units={"u1": {"id": "u1", "type": "text", "content": "脉冲持续时间决定热输入。"}},
    )
    proposals = [{
        "proposal_kind": "OBJECT_CONCEPT", "concept_type": "object",
        "canonical_name": "脉冲持续时间", "support": 9,
        "source_state_ids": ["s1"], "context_package_ids": ["p1"],
        "raw_expressions": ["脉冲持续时间"],
    }]

    rows = build_pending_concepts_from_proposals(
        proposals, inputs, MemorySnapshot.build([], []),
    )

    assert len(rows) == 1
    assert rows[0]["occurrence_count"] == 9
    assert rows[0]["candidate_origin"] == "UNPROCESSED_OBJECT"
    assert rows[0]["evidence_ids"] == ["u1"]
    assert rows[0]["evidence"][0]["text"] == "脉冲持续时间决定热输入。"


def test_serial_admission_second_candidate_sees_first_and_merges(tmp_path):
    concepts = [
        {
            "concept_id": "arc", "canonical_name": "电弧", "aliases": [],
            "definition": "气体放电现象", "evidence_ids": ["u1"],
        },
        {
            "concept_id": "arc_light", "canonical_name": "弧光", "aliases": [],
            "definition": "电弧的另一名称", "evidence_ids": ["u2"],
        },
    ]
    candidates = [
        {
            **row, "candidate_id": row["concept_id"], "occurrence_count": 2,
            "evidence": [{"evidence_id": row["evidence_ids"][0], "text": row["definition"]}],
        }
        for row in concepts
    ]
    client = _SerialFakeClient()
    runner = SerialConceptAdmissionRunner(
        {"fake": {"model": "fake-model"}}, "fake",
        "prompts/semantic_alignment/concept_admission_v2.yaml", tmp_path,
        client=client,
    )

    reviews, events, report = runner.run(
        candidates, concepts=concepts, relations=[],
    )

    assert [row["decision"] for row in reviews] == ["APPROVE", "MERGE"]
    assert len(set(client.memory_versions)) == 2
    assert report["approved_count"] == 1
    assert report["merged_count"] == 1
    final = MemorySnapshot.build(concepts, [], events)
    assert len(final.concepts) == 1
    assert final.concepts[0]["concept_id"] == "arc"
    assert "弧光" in final.concepts[0]["aliases"]
