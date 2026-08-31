from __future__ import annotations

import json
from pathlib import Path
import tempfile

from constella.semantic_alignment.runner import SemanticAlignmentRunner


class FakeClient:
    def __init__(self, output=None):
        self.output = output
        self.calls = 0
        self.tiers: list[str] = []

    def complete(self, *args, **kwargs):
        self.calls += 1
        messages = args[1]
        package = json.loads(messages[1]["content"])
        self.tiers.append(package["tier"])
        output = self.output
        if output is None:
            output = {"interpretations": [{
                "object_id": case["object_id"],
                "decision": "ATOMIC",
                "core_objects": [{"text": case["name"], "concept_id": None}],
                "embedded_states": [],
                "qualifiers": [],
            } for case in package["cases"]]}
        return {"choices": [{"message": {"content": json.dumps(output, ensure_ascii=False)}}]}


class CorrectingClient:
    def __init__(self):
        self.calls = 0
        self.second_messages = None

    def complete(self, *args, **_kwargs):
        self.calls += 1
        messages = args[1]
        package = json.loads(messages[1]["content"])
        case = package["cases"][0]
        if self.calls == 1:
            concept_id = "not_allowed"
        else:
            self.second_messages = messages
            concept_id = None
        output = {"interpretations": [{
            "object_id": case["object_id"], "decision": "ATOMIC",
            "core_objects": [{"text": case["name"], "concept_id": concept_id}],
            "embedded_states": [], "qualifiers": [],
        }]}
        return {"choices": [{"message": {"content": json.dumps(output, ensure_ascii=False)}}]}


def _package(tier="H1", object_id="o1", candidates=None, *, long_tail=False):
    return {
        "package_id": f"p_{tier}_{object_id}",
        "package_type": "object_alignment",
        "tier": tier,
        "memory_version": "m1",
        "cases": [{
            "object_id": object_id,
            "name": "电流",
            "candidates": candidates or [],
            "long_tail_fallback_required": long_tail,
        }],
    }


def test_runner_caches_valid_interpretation():
    client = FakeClient()
    with tempfile.TemporaryDirectory() as directory:
        runner = SemanticAlignmentRunner(
            {"fake": {"model": "fake"}}, "fake", Path("prompts/semantic_alignment"), directory,
            client=client,
        )
        first, first_report = runner.run([_package()])
        second, second_report = runner.run([_package()])
    assert first[0]["status"] == "success"
    assert first_report["protocol_success_rate"] == 1.0
    assert second_report["cached_count"] == 1
    assert client.calls == 1


def test_runner_processes_high_confidence_tier_before_lower_tier():
    client = FakeClient()
    with tempfile.TemporaryDirectory() as directory:
        runner = SemanticAlignmentRunner(
            {"fake": {"model": "fake"}}, "fake", Path("prompts/semantic_alignment"), directory,
            workers=2, client=client,
        )
        results, report = runner.run([_package("H2", "o2"), _package("H1", "o1")])
    assert len(results) == 2
    assert client.tiers == ["H1", "H2"]
    assert [row["tier"] for row in report["tier_reports"]] == ["H1", "H2"]


def test_candidate_from_another_case_is_rejected():
    package = {
        "package_id": "p1", "package_type": "object_alignment", "tier": "H1", "memory_version": "m1",
        "cases": [
            {"object_id": "o1", "name": "电流", "candidates": []},
            {"object_id": "o2", "name": "焊接电流", "candidates": [{"id": "c1"}]},
        ],
    }
    output = {"interpretations": [
        {"object_id": "o1", "decision": "ATOMIC", "core_objects": [{"text": "电流", "concept_id": "c1"}], "embedded_states": [], "qualifiers": []},
        {"object_id": "o2", "decision": "ATOMIC", "core_objects": [{"text": "焊接电流", "concept_id": "c1"}], "embedded_states": [], "qualifiers": []},
    ]}
    client = FakeClient(output)
    with tempfile.TemporaryDirectory() as directory:
        runner = SemanticAlignmentRunner(
            {"fake": {"model": "fake"}}, "fake", Path("prompts/semantic_alignment"), directory,
            client=client,
        )
        results, report = runner.run([package])
    assert results[0]["status"] == "failed"
    assert report["protocol_success_rate"] == 0.0
    assert client.calls == 3


def test_retry_includes_invalid_output_and_specific_validation_error():
    client = CorrectingClient()
    with tempfile.TemporaryDirectory() as directory:
        runner = SemanticAlignmentRunner(
            {"fake": {"model": "fake"}}, "fake", Path("prompts/semantic_alignment"), directory,
            client=client,
        )
        results, _report = runner.run([_package()])

    assert results[0]["status"] == "success"
    assert [row["role"] for row in client.second_messages[-2:]] == ["assistant", "user"]
    assert "not_allowed" in client.second_messages[-1]["content"]


def test_expression_only_requires_empty_structure_arrays():
    output = {"interpretations": [{
        "object_id": "o1",
        "decision": "EXPRESSION_ONLY",
        "core_objects": [{"text": "电流", "concept_id": None}],
        "embedded_states": [],
        "qualifiers": [],
    }]}
    client = FakeClient(output)
    with tempfile.TemporaryDirectory() as directory:
        runner = SemanticAlignmentRunner(
            {"fake": {"model": "fake"}}, "fake", Path("prompts/semantic_alignment"), directory,
            client=client,
        )
        results, _report = runner.run([_package()])
    assert results[0]["status"] == "failed"


def test_long_tail_object_cannot_propose_a_new_atomic_object():
    client = FakeClient()
    with tempfile.TemporaryDirectory() as directory:
        runner = SemanticAlignmentRunner(
            {"fake": {"model": "fake"}}, "fake", Path("prompts/semantic_alignment"), directory,
            client=client,
        )
        results, _report = runner.run([_package(long_tail=True)])

    assert results[0]["status"] == "failed"
    assert results[0]["attempt_count"] == 3
    assert client.calls == 3


def test_long_tail_object_can_fallback_to_upper_concept_and_state():
    output = {"interpretations": [{
        "object_id": "o1", "decision": "DECOMPOSED",
        "core_objects": [{"text": "脉冲TIG焊", "concept_id": "parent"}],
        "embedded_states": [{
            "role": "OBJECT_INTRINSIC_STATE", "subject_text": "脉冲TIG焊",
            "state_text": "低频",
        }],
        "qualifiers": [],
    }]}
    client = FakeClient(output)
    with tempfile.TemporaryDirectory() as directory:
        runner = SemanticAlignmentRunner(
            {"fake": {"model": "fake"}}, "fake", Path("prompts/semantic_alignment"), directory,
            client=client,
        )
        results, _report = runner.run([
            _package(candidates=[{"id": "parent"}], long_tail=True),
        ])

    assert results[0]["status"] == "success"
