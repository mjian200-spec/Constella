from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import yaml

from constella.semantic_alignment.concept_admission import (
    SerialConceptAdmissionRunner,
    _CHECKS,
)
from constella.semantic_alignment.packages import AlignmentInputs
from constella.semantic_alignment.registry import MemorySnapshot

ROOT = Path(__file__).resolve().parents[2]
PROMPT_PATH = ROOT / "prompts" / "semantic_alignment" / "concept_final_review_v1.yaml"


def _load_runner_module():
    module_path = ROOT / "scripts" / "run_semantic_alignment_loop.py"
    spec = importlib.util.spec_from_file_location("run_semantic_alignment_loop", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stage_plan_pairs_each_tier_with_its_own_admission_and_ends_with_final_review():
    module = _load_runner_module()
    assert module.STAGE_PLAN == (
        ("INITIAL", None), ("H1", "H1"), ("H2", "H2"), ("H3", "H3"), (None, None),
    )
    assert module.selected_stage_plan(1) == (("INITIAL", None),)
    assert module.selected_stage_plan(3) == module.STAGE_PLAN[:3]
    assert module.selected_stage_plan(5) == module.STAGE_PLAN


def test_final_review_prompt_schema_stays_compatible_with_admission_validation():
    prompt = yaml.safe_load(PROMPT_PATH.read_text(encoding="utf-8"))
    assert {"id", "version", "system"} <= set(prompt)
    assert prompt["id"] == "semantic_serial_concept_final_review"
    # The review offers no DEFER: it is the terminal decision.
    assert '"decision":"APPROVE|MERGE|REJECT"' in prompt["system"].replace(" ", "")
    assert "禁止DEFER" in prompt["system"]
    assert "同一source_state_id只能算一次" in prompt["system"]
    assert "occurrence_at_defer" not in prompt["system"]
    for key in _CHECKS:
        assert key in prompt["system"]


def _capture_client():
    captured = {}

    class _Client:
        def complete(self, model_key, messages, **kwargs):
            package = json.loads(messages[1]["content"])
            captured["package"] = package
            candidate = package["candidate"]
            return {"choices": [{"message": {"content": json.dumps({
                "concept_id": candidate["concept_id"],
                "decision": "DEFER",
                "target_concept_id": None,
                "selected_type": None,
                "canonical_name": None,
                "aliases": [],
                "definition": None,
                "evidence_ids": [],
                "relations": [],
                "missing_relation_concepts": [],
                "confidence": "LOW",
                "boundary_checks": {key: False for key in _CHECKS},
                "reason": "延后后证据仍不足。",
            }, ensure_ascii=False)}}]}

    return _Client(), captured


def test_final_review_candidate_carries_parked_recurrence_evidence(tmp_path):
    candidates = [{
        "concept_id": "parked_arc", "candidate_id": "parked_arc",
        "canonical_name": "弧光", "aliases": [],
        "definition": "焊接电弧发出的可见光",
        "occurrence_count": 6,
        "defer_history": [{"cycle": 1, "decision": "DEFER", "reason": "证据不足"}],
        "raw_object_variants": [
            {"text": "弧光", "count": 3}, {"text": "电弧光", "count": 2},
        ],
        "evidence": [{"evidence_id": "u1", "text": "弧光即电弧发出的光。"}],
    }]
    client, captured = _capture_client()
    runner = SerialConceptAdmissionRunner(
        {"fake": {"model": "fake-model"}}, "fake", PROMPT_PATH, tmp_path,
        client=client, allow_defer=False,
    )

    reviews, _events, report = runner.run(
        candidates, concepts=candidates, relations=[],
    )

    package_candidate = captured["package"]["candidate"]
    assert "occurrence_at_defer" not in package_candidate
    assert package_candidate["occurrence_count"] == 6
    assert package_candidate["defer_history"][0]["cycle"] == 1
    assert package_candidate["raw_object_variants"] == [
        {"text": "弧光", "count": 3}, {"text": "电弧光", "count": 2},
    ]
    # The final review must be terminal: a DEFER output becomes REJECT.
    assert reviews[0]["status"] == "REJECT"
    assert reviews[0]["gate_reason"] == "re_review_does_not_allow_defer"
    assert report["rejected_count"] == 1


def test_final_review_receives_the_final_library_for_synonym_comparison(tmp_path):
    library = [{
        "concept_id": "arc", "canonical_name": "电弧", "aliases": [],
        "definition": "气体放电现象", "evidence_ids": ["u1"],
    }]
    parked = [{
        "concept_id": "parked_weld_arc", "candidate_id": "parked_weld_arc",
        "canonical_name": "焊接电弧", "aliases": [],
        "definition": "焊接过程中产生的电弧",
        "occurrence_count": 5,
        "defer_history": [{"cycle": 2, "decision": "DEFER", "reason": "置信度不足"}],
        "raw_object_variants": [{"text": "焊接电弧", "count": 5}],
        "evidence": [{"evidence_id": "u1", "text": "焊接电弧由焊接电源产生。"}],
    }]
    client, captured = _capture_client()
    runner = SerialConceptAdmissionRunner(
        {"fake": {"model": "fake-model"}}, "fake", PROMPT_PATH, tmp_path,
        client=client, allow_defer=False,
    )

    approved_arc = [{
        "event_id": "ev_arc", "status": "APPROVED",
        "concept": {
            "concept_id": "arc", "canonical_name": "电弧", "aliases": [],
            "type": "object", "definition": "气体放电现象", "evidence_ids": ["u1"],
        },
    }]
    runner.run(
        parked, concepts=[*library, *parked], relations=[],
        reviewed_memory=approved_arc,
    )

    # The registered 电弧 shares the 电弧 bigram with the parked candidate, so
    # the final library is recalled for the near-synonym comparison.
    registered = captured["package"]["registered_candidates"]
    assert any(row["name"] == "电弧" for row in registered)


def test_parked_evidence_is_unique_and_only_uses_deterministic_identity():
    module = _load_runner_module()
    parked = {
        "arc": {
            "concept_id": "arc", "canonical_name": "电弧", "aliases": ["焊接电弧"],
            "source_state_ids": ["s1"], "occurrence_count": 1,
            "raw_object_variants": [{"text": "电弧", "count": 1}],
            "raw_object_variant_source_ids": {"电弧": ["s1"]},
            "source_package_ids": [], "evidence_ids": [],
        },
    }
    proposals = [
        {
            "proposal_kind": "OBJECT_CONCEPT", "concept_type": "object",
            "concept_id": "arc", "canonical_name": "电弧", "support": 99,
            "source_state_ids": ["s1"], "raw_expressions": ["电弧"],
            "raw_expression_source_state_ids": {"电弧": ["s1"]},
            "context_package_ids": [],
        },
        {
            "proposal_kind": "OBJECT_CONCEPT", "concept_type": "object",
            "canonical_name": "焊接电弧", "support": 50,
            "source_state_ids": ["s2"], "raw_expressions": ["焊接电弧"],
            "raw_expression_source_state_ids": {"焊接电弧": ["s2"]},
            "context_package_ids": [],
        },
        {
            "proposal_kind": "OBJECT_CONCEPT", "concept_type": "object",
            "canonical_name": "弧光", "support": 50,
            "source_state_ids": ["s3"], "raw_expressions": ["弧光"],
            "raw_expression_source_state_ids": {"弧光": ["s3"]},
            "context_package_ids": [],
        },
    ]
    inputs = AlignmentInputs(
        concepts=[], relations=[], rules=[], context_packages={}, units={},
    )

    module._accumulate_parked(proposals, parked, inputs)

    assert parked["arc"]["source_state_ids"] == ["s1", "s2"]
    assert parked["arc"]["occurrence_count"] == 2
    assert parked["arc"]["raw_object_variants"] == [
        {"text": "焊接电弧", "count": 1}, {"text": "电弧", "count": 1},
    ]


def test_final_library_export_omits_legacy_state_concepts():
    module = _load_runner_module()
    memory = MemorySnapshot.build([
        {
            "concept_id": "arc", "canonical_name": "电弧", "aliases": [],
            "type": "object", "registration_status": "APPROVED",
        },
        {
            "concept_id": "hot", "canonical_name": "过热", "aliases": [],
            "type": "state", "registration_status": "APPROVED",
        },
    ], [])

    concepts, relations, candidates = module._registered_library(memory)

    assert [row["concept_id"] for row in concepts] == ["arc"]
    assert relations == []
    assert candidates == []
