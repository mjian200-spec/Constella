from __future__ import annotations

import json
import threading
import time

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


class _ConcurrentFakeClient:
    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def complete(self, _model_key, messages, **_kwargs):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.05)
            package = json.loads(messages[1]["content"])
            output = _decision(package["candidate"])
            return {
                "model": "fake-model",
                "choices": [{"message": {"content": json.dumps(output, ensure_ascii=False)}}],
            }
        finally:
            with self.lock:
                self.active -= 1


class _HierarchyFakeClient:
    def complete(self, _model_key, messages, **_kwargs):
        package = json.loads(messages[1]["content"])
        candidate = package["candidate"]
        output = _decision(candidate)
        if candidate["concept_id"] == "parent":
            child = next(row for row in package["registered_candidates"] if row["id"] == "child")
            output["relations"] = [{
                "type": "IS_A", "direction": "INCOMING",
                "target_concept_id": child["id"], "evidence_ids": ["u1"],
            }]
        return {
            "model": "fake-model",
            "choices": [{"message": {"content": json.dumps(output, ensure_ascii=False)}}],
        }


class _GeneratedConceptFakeClient:
    def __init__(self):
        self.calls = 0

    def complete(self, _model_key, messages, **_kwargs):
        self.calls += 1
        package = json.loads(messages[1]["content"])
        candidate = package["candidate"]
        output = _decision(candidate)
        if candidate["canonical_name"] == "熔池":
            output["missing_relation_concepts"] = [{
                "canonical_name": "头部", "aliases": [], "definition": "熔池的前部。",
                "type": "object", "evidence_ids": ["u1"],
            }]
        return {
            "model": "fake-model",
            "choices": [{"message": {"content": json.dumps(output, ensure_ascii=False)}}],
        }


class _RecoveringFakeClient:
    def __init__(self):
        self.valid = False

    def complete(self, _model_key, messages, **_kwargs):
        package = json.loads(messages[1]["content"])
        content = (
            json.dumps(_decision(package["candidate"]), ensure_ascii=False)
            if self.valid else '{"decision":"BROKEN"}'
        )
        return {"model": "fake-model", "choices": [{"message": {"content": content}}]}


class _RejectingFakeClient:
    def complete(self, _model_key, messages, **_kwargs):
        package = json.loads(messages[1]["content"])
        output = _decision(package["candidate"])
        output.update({"decision": "REJECT", "selected_type": None})
        return {"model": "fake-model", "choices": [{"message": {"content": json.dumps(output)}}]}


class _DeferringFakeClient:
    def complete(self, _model_key, messages, **_kwargs):
        package = json.loads(messages[1]["content"])
        output = _decision(package["candidate"])
        output.update({"decision": "DEFER", "selected_type": None})
        return {"model": "fake-model", "choices": [{"message": {"content": json.dumps(output)}}]}


class _MediumFakeClient:
    def complete(self, _model_key, messages, **_kwargs):
        package = json.loads(messages[1]["content"])
        output = _decision(package["candidate"])
        output.update({"confidence": "MEDIUM"})
        return {"model": "fake-model", "choices": [{"message": {"content": json.dumps(output)}}]}


class _UnderevidencedFakeClient:
    def complete(self, _model_key, messages, **_kwargs):
        package = json.loads(messages[1]["content"])
        output = _decision(package["candidate"])
        output["boundary_checks"]["evidence_sufficient"] = False
        return {"model": "fake-model", "choices": [{"message": {"content": json.dumps(output)}}]}


def test_initial_candidates_include_zero_occurrence_extracted_concepts():
    inputs = AlignmentInputs(
        concepts=[
            {"concept_id": "arc", "canonical_name": "电弧", "aliases": [], "evidence_ids": ["u1"]},
            {"concept_id": "rare", "canonical_name": "稀有概念", "aliases": [], "evidence_ids": ["u2"]},
        ],
        relations=[{
            "relation_id": "r1", "child_concept_id": "arc", "parent_concept_id": "rare",
            "type": "IS_A", "evidence_ids": ["u3"],
        }],
        rules=[{
            "id": "r1", "conditions": [],
            "antecedents": [{"id": "s1", "object": "电弧"}], "consequents": [],
        }],
        context_packages={},
        units={
            "u1": {"id": "u1", "type": "text", "content": "电弧是一种气体放电。"},
            "u2": {"id": "u2", "type": "text", "content": "这里定义稀有概念。"},
            "u3": {"id": "u3", "type": "text", "content": "电弧属于稀有概念。"},
        },
    )

    rows = build_initial_pending_concepts(
        inputs, MemorySnapshot.build(inputs.concepts, inputs.relations),
    )

    assert [row["concept_id"] for row in rows] == ["arc", "rare"]
    assert [row["occurrence_count"] for row in rows] == [1, 0]
    assert rows[1]["evidence"][0]["evidence_id"] == "u2"
    assert rows[0]["catalog_relation_hints"][0]["direction"] == "OUTGOING"
    assert rows[1]["catalog_relation_hints"][0]["direction"] == "INCOMING"


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
        "source_state_ids": [f"s{index}" for index in range(9)],
        "context_package_ids": ["p1"],
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


def test_alignment_candidate_counts_unique_source_states_across_repeated_proposals():
    inputs = AlignmentInputs(
        concepts=[], relations=[], rules=[], context_packages={}, units={},
    )
    proposal = {
        "proposal_kind": "OBJECT_CONCEPT", "concept_type": "object",
        "canonical_name": "电弧", "support": 20,
        "source_state_ids": ["s1"], "source_rule_ids": ["r1"],
        "raw_expressions": ["电弧"],
        "raw_expression_source_state_ids": {"电弧": ["s1"]},
    }

    rows = build_pending_concepts_from_proposals(
        [proposal, proposal], inputs, MemorySnapshot.build([], []),
    )

    assert rows[0]["occurrence_count"] == 1
    assert rows[0]["source_rule_count"] == 1
    assert rows[0]["raw_object_variants"] == [{"text": "电弧", "count": 1}]


def test_state_and_action_proposals_do_not_enter_object_concept_admission():
    inputs = AlignmentInputs(
        concepts=[], relations=[], rules=[], context_packages={}, units={},
    )

    rows = build_pending_concepts_from_proposals([{
        "proposal_kind": "OBJECT_CONCEPT", "concept_type": "state",
        "canonical_name": "升高", "support": 100,
    }], inputs, MemorySnapshot.build([], []))

    assert rows == []


def test_low_rank_single_occurrence_object_proposals_do_not_enter_admission():
    inputs = AlignmentInputs(
        concepts=[], relations=[], rules=[], context_packages={}, units={},
    )
    proposals = [{
        "proposal_kind": "OBJECT_CONCEPT", "concept_type": "object",
        "canonical_name": f"长尾对象{index:02d}", "support": 1,
    } for index in range(31)]

    rows = build_pending_concepts_from_proposals(
        proposals, inputs, MemorySnapshot.build([], []),
    )

    assert len(rows) == 6
    assert {row["rank_confidence"] for row in rows} == {"HIGH", "MEDIUM"}


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
        workers=2, client=client,
    )

    reviews, events, report = runner.run(
        candidates, concepts=concepts, relations=[],
    )

    assert [row["decision"] for row in reviews] == ["APPROVE", "MERGE"]
    assert len(set(client.memory_versions)) == 2
    assert report["approved_count"] == 1
    assert report["merged_count"] == 1
    assert report["stale_result_count"] == 1
    assert report["submitted_count"] == 3
    final = MemorySnapshot.build(concepts, [], events)
    assert len(final.concepts) == 1
    assert final.concepts[0]["concept_id"] == "arc"
    assert "弧光" in final.concepts[0]["aliases"]


def test_independent_admissions_are_reviewed_concurrently(tmp_path):
    concepts = [
        {
            "concept_id": "arc", "canonical_name": "电弧", "aliases": [],
            "definition": "气体放电现象", "evidence_ids": ["u1"],
        },
        {
            "concept_id": "torch", "canonical_name": "焊枪", "aliases": [],
            "definition": "输送焊丝的设备", "evidence_ids": ["u2"],
        },
    ]
    candidates = [
        {
            **row, "candidate_id": row["concept_id"], "occurrence_count": 2,
            "evidence": [{"evidence_id": row["evidence_ids"][0], "text": row["definition"]}],
        }
        for row in concepts
    ]
    client = _ConcurrentFakeClient()
    progress = []
    runner = SerialConceptAdmissionRunner(
        {"fake": {"model": "fake-model"}}, "fake",
        "prompts/semantic_alignment/concept_admission_v2.yaml", tmp_path,
        workers=2, client=client, progress=progress.append,
    )

    reviews, _events, report = runner.run(
        candidates, concepts=concepts, relations=[],
    )

    assert [row["decision"] for row in reviews] == ["APPROVE", "APPROVE"]
    assert client.max_active == 2
    assert report["workers"] == 2
    assert report["batch_count"] == 1
    assert report["submitted_count"] == 2
    assert report["stale_result_count"] == 0
    assert progress[-1]["processed"] == 2


def test_serial_admission_activates_relation_when_other_endpoint_is_registered(tmp_path):
    concepts = [
        {"concept_id": "child", "canonical_name": "电弧", "aliases": [], "definition": "放电现象", "evidence_ids": ["u1"]},
        {"concept_id": "parent", "canonical_name": "气体放电", "aliases": [], "definition": "气体中的放电", "evidence_ids": ["u1"]},
    ]
    hints = {
        "child": [{"type": "IS_A", "direction": "OUTGOING", "other_concept_id": "parent", "evidence_ids": ["u1"]}],
        "parent": [{"type": "IS_A", "direction": "INCOMING", "other_concept_id": "child", "evidence_ids": ["u1"]}],
    }
    candidates = [{
        **row, "candidate_id": row["concept_id"], "occurrence_count": 1,
        "evidence": [{"evidence_id": "u1", "text": "电弧是一种气体放电。"}],
        "catalog_relation_hints": hints[row["concept_id"]],
    } for row in concepts]
    relations = [{
        "relation_id": "candidate_r1", "child_concept_id": "child",
        "parent_concept_id": "parent", "type": "IS_A",
    }]
    runner = SerialConceptAdmissionRunner(
        {"fake": {"model": "fake-model"}}, "fake",
        "prompts/semantic_alignment/concept_admission_v2.yaml", tmp_path,
        workers=2, client=_HierarchyFakeClient(),
    )

    _reviews, events, report = runner.run(
        candidates, concepts=concepts, relations=relations,
    )

    final = MemorySnapshot.build(concepts, relations, events)
    approved_relations = [
        row for row in final.relations if row.get("registration_status") == "APPROVED"
    ]
    assert len(approved_relations) == 1
    assert approved_relations[0]["child_concept_id"] == "child"
    assert approved_relations[0]["parent_concept_id"] == "parent"
    assert report["stale_result_count"] == 1
    assert report["submitted_count"] == 3
    assert report["library_audit"]["relation_counts"] == {"IS_A": 1}


def test_generated_generic_relation_name_is_context_completed_and_checkpoint_resumes(tmp_path):
    concepts = [
        {
            "concept_id": "pool", "canonical_name": "熔池", "aliases": [],
            "definition": "焊接中的液态金属区域。", "evidence_ids": ["u1"],
        },
    ]
    candidates = [{
        **concepts[0], "candidate_id": "pool", "occurrence_count": 1,
        "evidence": [{"evidence_id": "u1", "text": "熔池头部是熔池的前部。"}],
    }]
    client = _GeneratedConceptFakeClient()
    runner = SerialConceptAdmissionRunner(
        {"fake": {"model": "fake-model"}}, "fake",
        "prompts/semantic_alignment/concept_admission_v2.yaml", tmp_path,
        client=client,
    )

    reviews, events, report = runner.run(candidates, concepts=concepts, relations=[])

    assert [row["canonical_name"] for row in reviews] == ["熔池", "熔池头部"]
    assert report["generated_pending_concept_count"] == 1
    first_call_count = client.calls
    resumed_reviews, resumed_events, resumed_report = runner.run(
        candidates, concepts=concepts, relations=[],
    )
    assert resumed_reviews == reviews
    assert resumed_events == events
    assert resumed_report == report
    assert client.calls == first_call_count


def test_failed_checkpoint_candidate_is_requeued_on_resume(tmp_path):
    concept = {
        "concept_id": "arc", "canonical_name": "电弧", "aliases": [],
        "definition": "气体放电现象。", "evidence_ids": ["u1"],
    }
    candidate = {
        **concept, "candidate_id": "arc", "occurrence_count": 1,
        "evidence": [{"evidence_id": "u1", "text": "电弧是气体放电现象。"}],
    }
    client = _RecoveringFakeClient()
    runner = SerialConceptAdmissionRunner(
        {"fake": {"model": "fake-model"}}, "fake",
        "prompts/semantic_alignment/concept_admission_v2.yaml", tmp_path, client=client,
    )
    reviews, _events, report = runner.run([candidate], concepts=[concept], relations=[])
    assert reviews[0]["decision"] == "FAILED"
    assert report["failed_count"] == 1

    client.valid = True
    reviews, _events, report = runner.run([candidate], concepts=[concept], relations=[])
    assert reviews[0]["decision"] == "APPROVE"
    assert report["failed_count"] == 0


def test_rejected_review_is_remembered_without_becoming_registered(tmp_path):
    concept = {
        "concept_id": "noise", "canonical_name": "临时编号", "aliases": [],
        "definition": "一次性编号。", "evidence_ids": ["u1"],
    }
    candidate = {
        **concept, "candidate_id": "noise", "occurrence_count": 1,
        "evidence": [{"evidence_id": "u1", "text": "临时编号仅用于本次记录。"}],
    }
    runner = SerialConceptAdmissionRunner(
        {"fake": {"model": "fake-model"}}, "fake",
        "prompts/semantic_alignment/concept_admission_v2.yaml", tmp_path,
        client=_RejectingFakeClient(),
    )

    reviews, events, report = runner.run([candidate], concepts=[concept], relations=[])
    memory = MemorySnapshot.build([concept], [], events)

    assert reviews[0]["decision"] == "REJECT"
    assert events[0]["status"] == "REJECT"
    assert "noise" in memory.reviewed_concept_ids
    assert memory.concepts[0]["registration_status"] == "CANDIDATE"
    assert build_initial_pending_concepts(
        AlignmentInputs(
            concepts=[concept], relations=[], rules=[], context_packages={}, units={},
        ),
        memory,
    ) == []
    assert report["rejected_count"] == 1


def test_initial_candidates_skip_state_type_concepts():
    inputs = AlignmentInputs(
        concepts=[
            {"concept_id": "arc", "canonical_name": "电弧", "aliases": [], "type": "object"},
            {"concept_id": "increase", "canonical_name": "增大", "aliases": [], "type": "state"},
        ],
        relations=[], rules=[], context_packages={}, units={},
    )

    rows = build_initial_pending_concepts(
        inputs, MemorySnapshot.build(inputs.concepts, inputs.relations),
    )

    assert [row["concept_id"] for row in rows] == ["arc"]


def test_defer_is_parked_and_not_terminal(tmp_path):
    concept = {
        "concept_id": "arc", "canonical_name": "电弧", "aliases": [],
        "definition": "气体放电现象。", "evidence_ids": ["u1"], "type": "object",
    }
    candidate = {
        **concept, "candidate_id": "arc", "occurrence_count": 1,
        "evidence": [{"evidence_id": "u1", "text": "电弧是气体放电现象。"}],
    }
    runner = SerialConceptAdmissionRunner(
        {"fake": {"model": "fake-model"}}, "fake",
        "prompts/semantic_alignment/concept_admission_v2.yaml", tmp_path,
        client=_DeferringFakeClient(),
    )

    reviews, events, report = runner.run([candidate], concepts=[concept], relations=[])
    memory = MemorySnapshot.build([concept], [], events)

    assert reviews[0]["decision"] == "DEFER"
    assert reviews[0]["status"] == "DEFER"
    assert reviews[0]["gate_reason"] == "model_defer"
    assert events[0]["status"] == "DEFER"
    assert "arc" not in memory.reviewed_concept_ids
    assert report["deferred_count"] == 1


def test_final_rereview_converts_defer_to_reject(tmp_path):
    concept = {
        "concept_id": "arc", "canonical_name": "电弧", "aliases": [],
        "definition": "气体放电现象。", "evidence_ids": ["u1"], "type": "object",
    }
    candidate = {
        **concept, "candidate_id": "arc", "occurrence_count": 1,
        "evidence": [{"evidence_id": "u1", "text": "电弧是气体放电现象。"}],
        "defer_history": [{"cycle": 0, "decision": "DEFER", "reason": "证据不足"}],
    }
    runner = SerialConceptAdmissionRunner(
        {"fake": {"model": "fake-model"}}, "fake",
        "prompts/semantic_alignment/concept_admission_v2.yaml", tmp_path,
        client=_DeferringFakeClient(), allow_defer=False,
    )

    reviews, events, report = runner.run([candidate], concepts=[concept], relations=[])
    memory = MemorySnapshot.build([concept], [], events)

    assert reviews[0]["decision"] == "REJECT"
    assert reviews[0]["status"] == "REJECT"
    assert reviews[0]["gate_reason"] == "re_review_does_not_allow_defer"
    assert events[0]["status"] == "REJECT"
    assert "arc" in memory.reviewed_concept_ids
    assert report["deferred_count"] == 0
    assert report["rejected_count"] == 1


def test_initial_candidates_skip_registered_duplicate_names_but_not_definition_substrings():
    inputs = AlignmentInputs(
        concepts=[
            {
                "concept_id": "arc", "canonical_name": "电弧", "aliases": [],
                "type": "object", "definition": "气体放电现象。",
                "registration_status": "APPROVED",
            },
            {
                "concept_id": "arc_light", "canonical_name": "弧光", "aliases": [],
                "type": "object", "definition": "焊接中电弧的气体放电现象。",
                "registration_status": "CANDIDATE",
            },
            {
                "concept_id": "same_name", "canonical_name": "电弧", "aliases": ["火弧"],
                "type": "object", "definition": "另一个电弧。",
                "registration_status": "CANDIDATE",
            },
        ],
        relations=[], rules=[], context_packages={}, units={},
    )

    rows = build_initial_pending_concepts(
        inputs, MemorySnapshot.build(inputs.concepts, inputs.relations),
    )

    assert [row["concept_id"] for row in rows] == ["arc_light"]


def test_pending_proposals_are_blocked_by_terminal_names():
    inputs = AlignmentInputs(
        concepts=[], relations=[], rules=[], context_packages={}, units={},
    )
    proposals = [{
        "proposal_kind": "OBJECT_CONCEPT", "concept_type": "object",
        "canonical_name": "异种材料接头", "support": 9,
        "source_state_ids": ["s1"], "context_package_ids": ["p1"],
        "raw_expressions": ["异种材料接头"],
    }]

    rows = build_pending_concepts_from_proposals(
        proposals, inputs, MemorySnapshot.build([], []),
        blocked_names={"异种材料接头"},
    )

    assert rows == []


def test_pending_proposals_skip_names_owned_by_registered_concepts():
    inputs = AlignmentInputs(
        concepts=[], relations=[], rules=[], context_packages={}, units={},
    )
    proposals = [{
        "proposal_kind": "OBJECT_CONCEPT", "concept_type": "object",
        "canonical_name": "异种材料接头", "support": 9,
        "source_state_ids": ["s1"], "context_package_ids": ["p1"],
        "raw_expressions": ["异种材料接头"],
    }]
    memory = MemorySnapshot.build([{
        "concept_id": "c_joint", "canonical_name": "异种材料接头", "aliases": [],
        "registration_status": "APPROVED",
    }], [], [])

    rows = build_pending_concepts_from_proposals(proposals, inputs, memory)

    assert rows == []


def test_unresolved_object_proposals_are_admitted_as_object_concepts():
    inputs = AlignmentInputs(
        concepts=[], relations=[], rules=[], context_packages={}, units={},
    )
    proposals = [{
        "proposal_kind": "OBJECT_CONCEPT", "concept_type": "object",
        "canonical_name": "熔池", "support": 9,
        "source_state_ids": [f"s{index}" for index in range(9)],
        "context_package_ids": ["p1"],
        "raw_expressions": ["熔池"],
    }]

    rows = build_pending_concepts_from_proposals(
        proposals, inputs, MemorySnapshot.build([], []),
    )

    assert len(rows) == 1
    assert rows[0]["canonical_name"] == "熔池"
    assert rows[0]["occurrence_count"] == 9
    assert rows[0]["candidate_origin"] == "UNPROCESSED_OBJECT"


def test_unresolved_object_proposals_respect_blocked_names():
    inputs = AlignmentInputs(
        concepts=[], relations=[], rules=[], context_packages={}, units={},
    )
    proposals = [{
        "proposal_kind": "OBJECT_CONCEPT", "concept_type": "object",
        "canonical_name": "熔池", "support": 9,
        "source_state_ids": ["s1"], "context_package_ids": ["p1"],
        "raw_expressions": ["熔池"],
    }]

    rows = build_pending_concepts_from_proposals(
        proposals, inputs, MemorySnapshot.build([], []),
        blocked_names={"熔池"},
    )

    assert rows == []


def test_unresolved_object_candidate_definition_grounded_in_rule_context():
    inputs = AlignmentInputs(
        concepts=[], relations=[], rules=[{
            "id": "r1",
            "raw_expression": "熔池温度过高时，应降低焊接电流。",
            "conditions": [{
                "id": "s1", "object": "熔池",
                "raw_state": "温度过高", "normalized_state": "温度过高",
            }],
            "antecedents": [], "consequents": [],
        }], context_packages={}, units={},
    )
    proposals = [{
        "proposal_kind": "OBJECT_CONCEPT", "concept_type": "object",
        "canonical_name": "熔池", "support": 9,
        "source_state_ids": ["s1"], "context_package_ids": ["p1"],
        "raw_expressions": ["熔池"],
    }]

    rows = build_pending_concepts_from_proposals(
        proposals, inputs, MemorySnapshot.build([], []),
    )

    assert rows[0]["definition"] is not None
    assert "熔池" in rows[0]["definition"]
    assert "温度过高" in rows[0]["definition"]
    assert "应降低焊接电流" in rows[0]["definition"]


def test_low_confidence_approval_is_deferred_not_terminal(tmp_path):
    concept = {
        "concept_id": "arc", "canonical_name": "电弧", "aliases": [],
        "definition": "气体放电现象。", "evidence_ids": ["u1"], "type": "object",
    }
    candidate = {
        **concept, "candidate_id": "arc", "occurrence_count": 1,
        "evidence": [{"evidence_id": "u1", "text": "电弧是气体放电现象。"}],
    }
    runner = SerialConceptAdmissionRunner(
        {"fake": {"model": "fake-model"}}, "fake",
        "prompts/semantic_alignment/concept_admission_v2.yaml", tmp_path,
        client=_MediumFakeClient(),
    )

    reviews, events, report = runner.run([candidate], concepts=[concept], relations=[])
    memory = MemorySnapshot.build([concept], [], events)

    assert reviews[0]["decision"] == "APPROVE"
    assert reviews[0]["status"] == "DEFER"
    assert reviews[0]["gate_reason"] == "admission_requires_high_model_confidence"
    assert events[0]["status"] == "DEFER"
    assert "arc" not in memory.reviewed_concept_ids
    assert report["deferred_count"] == 1


def test_evidence_reuse_insufficient_approval_is_deferred(tmp_path):
    concept = {
        "concept_id": "arc", "canonical_name": "电弧", "aliases": [],
        "definition": "气体放电现象。", "evidence_ids": ["u1"], "type": "object",
    }
    candidate = {
        **concept, "candidate_id": "arc", "occurrence_count": 1,
        "evidence": [{"evidence_id": "u1", "text": "电弧是气体放电现象。"}],
    }
    runner = SerialConceptAdmissionRunner(
        {"fake": {"model": "fake-model"}}, "fake",
        "prompts/semantic_alignment/concept_admission_v2.yaml", tmp_path,
        client=_UnderevidencedFakeClient(),
    )

    reviews, events, report = runner.run([candidate], concepts=[concept], relations=[])
    memory = MemorySnapshot.build([concept], [], events)

    assert reviews[0]["decision"] == "APPROVE"
    assert reviews[0]["status"] == "DEFER"
    assert reviews[0]["gate_reason"] == "approval_requires_more_evidence_reuse"
    assert events[0]["status"] == "DEFER"
    assert "arc" not in memory.reviewed_concept_ids
    assert report["deferred_count"] == 1


def test_low_confidence_approval_is_rejected_in_final_rereview(tmp_path):
    concept = {
        "concept_id": "arc", "canonical_name": "电弧", "aliases": [],
        "definition": "气体放电现象。", "evidence_ids": ["u1"], "type": "object",
    }
    candidate = {
        **concept, "candidate_id": "arc", "occurrence_count": 1,
        "evidence": [{"evidence_id": "u1", "text": "电弧是气体放电现象。"}],
    }
    runner = SerialConceptAdmissionRunner(
        {"fake": {"model": "fake-model"}}, "fake",
        "prompts/semantic_alignment/concept_admission_v2.yaml", tmp_path,
        client=_MediumFakeClient(), allow_defer=False,
    )

    reviews, events, report = runner.run([candidate], concepts=[concept], relations=[])
    memory = MemorySnapshot.build([concept], [], events)

    assert reviews[0]["decision"] == "APPROVE"
    assert reviews[0]["status"] == "REJECT"
    assert events[0]["status"] == "REJECT"
    assert "arc" in memory.reviewed_concept_ids
    assert report["deferred_count"] == 0
    assert report["rejected_count"] == 1
