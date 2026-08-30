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


def _package(tier="H1", object_id="o1", candidates=None):
    return {
        "package_id": f"p_{tier}_{object_id}",
        "package_type": "object_alignment",
        "tier": tier,
        "memory_version": "m1",
        "cases": [{
            "object_id": object_id,
            "name": "电流",
            "candidates": candidates or [],
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
    assert client.calls == 2


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
