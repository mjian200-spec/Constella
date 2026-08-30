from __future__ import annotations

import json
from pathlib import Path
import tempfile

from constella.semantic_alignment.runner import SemanticAlignmentRunner


class FakeClient:
    def __init__(self, output):
        self.output = output
        self.calls = 0

    def complete(self, *args, **kwargs):
        self.calls += 1
        return {"choices": [{"message": {"content": json.dumps(self.output, ensure_ascii=False)}}]}


def test_runner_caches_valid_result():
    package = {
        "package_id": "p1", "package_type": "object_alignment",
        "cases": [{"object_id": "o1", "name": "电流", "candidates": [{"id": "c1"}]}],
    }
    client = FakeClient({"alignments": [{"object_id": "o1", "concept_id": "c1"}]})
    with tempfile.TemporaryDirectory() as directory:
        runner = SemanticAlignmentRunner(
            {"fake": {"model": "fake"}}, "fake", Path("prompts/semantic_alignment"), directory,
            client=client,
        )
        first, first_report = runner.run([package])
        second, second_report = runner.run([package])
    assert first[0]["status"] == "success"
    assert first_report["protocol_success_rate"] == 1.0
    assert second_report["cached_count"] == 1
    assert client.calls == 1


def test_state_protocol_retries_then_records_failure():
    package = {
        "package_id": "p1", "package_type": "state_normalization",
        "concept": {"id": "c1"}, "states": [{"id": "s1"}, {"id": "s2"}],
    }
    client = FakeClient({"groups": [{"canonical": "电流增大", "members": ["s1"]}], "exceptions": []})
    with tempfile.TemporaryDirectory() as directory:
        runner = SemanticAlignmentRunner(
            {"fake": {"model": "fake"}}, "fake", Path("prompts/semantic_alignment"), directory,
            client=client,
        )
        results, report = runner.run([package])
    assert results[0]["status"] == "failed"
    assert report["protocol_success_rate"] == 0.0
    assert client.calls == 2


def test_object_protocol_accepts_reparse_without_extra_fields():
    package = {
        "package_id": "p1", "package_type": "object_alignment",
        "cases": [{"object_id": "o1", "name": "200A", "candidates": []}],
    }
    client = FakeClient({"alignments": [{"object_id": "o1", "concept_id": "REPARSE"}]})
    with tempfile.TemporaryDirectory() as directory:
        runner = SemanticAlignmentRunner(
            {"fake": {"model": "fake"}}, "fake", Path("prompts/semantic_alignment"), directory,
            client=client,
        )
        results, report = runner.run([package])
    assert results[0]["status"] == "success"
    assert report["decision_coverage_rate"] == 1.0


def test_object_protocol_accepts_candidate_from_another_case_in_same_package():
    package = {
        "package_id": "p1", "package_type": "object_alignment",
        "cases": [
            {"object_id": "o1", "name": "电流", "candidates": []},
            {"object_id": "o2", "name": "焊接电流", "candidates": [{"id": "c1"}]},
        ],
    }
    client = FakeClient({"alignments": [
        {"object_id": "o1", "concept_id": "c1"},
        {"object_id": "o2", "concept_id": "c1"},
    ]})
    with tempfile.TemporaryDirectory() as directory:
        runner = SemanticAlignmentRunner(
            {"fake": {"model": "fake"}}, "fake", Path("prompts/semantic_alignment"), directory,
            client=client,
        )
        results, report = runner.run([package])
    assert results[0]["status"] == "success"
    assert report["protocol_success_rate"] == 1.0


def test_concept_review_only_accepts_proposed_pairs():
    package = {
        "package_id": "p1", "package_type": "concept_merge_review",
        "cases": [{"left": {"id": "c1"}, "right": {"id": "c2"}}],
    }
    client = FakeClient({"merge_pairs": [["c2", "c1"]]})
    with tempfile.TemporaryDirectory() as directory:
        runner = SemanticAlignmentRunner(
            {"fake": {"model": "fake"}}, "fake", Path("prompts/semantic_alignment"), directory,
            client=client,
        )
        results, report = runner.run([package])
    assert results[0]["status"] == "success"
    assert report["decision_coverage_rate"] == 1.0


def test_state_canonical_without_concept_name_is_quality_warning_not_protocol_failure():
    package = {
        "package_id": "p1", "package_type": "state_normalization",
        "concept": {"id": "c1", "name": "焊缝成形", "aliases": []},
        "states": [{"id": "s1"}],
    }
    client = FakeClient({"groups": [{"canonical": "良好成形", "members": ["s1"]}], "exceptions": []})
    with tempfile.TemporaryDirectory() as directory:
        runner = SemanticAlignmentRunner(
            {"fake": {"model": "fake"}}, "fake", Path("prompts/semantic_alignment"), directory,
            client=client,
        )
        results, report = runner.run([package])
    assert results[0]["status"] == "success"
    assert results[0]["quality"]["explicit_subject_group_count"] == 0
    assert report["explicit_subject_rate"] == 0.0


def test_state_object_alignment_accepts_only_package_candidates_or_terminal_values():
    package = {
        "package_id": "p1", "package_type": "state_object_alignment",
        "cases": [{"state_id": "s1", "candidates": [{"id": "c1"}]}],
    }
    client = FakeClient({"alignments": [{"state_id": "s1", "concept_id": "c1"}]})
    with tempfile.TemporaryDirectory() as directory:
        runner = SemanticAlignmentRunner(
            {"fake": {"model": "fake"}}, "fake", Path("prompts/semantic_alignment"), directory,
            client=client,
        )
        results, report = runner.run([package])
    assert results[0]["status"] == "success"
    assert report["decision_coverage_rate"] == 1.0


def test_runner_records_unhandled_package_failure_without_aborting_report():
    package = {
        "package_id": "p1", "package_type": "missing_prompt_type", "cases": [{"id": "x"}],
    }
    with tempfile.TemporaryDirectory() as directory:
        runner = SemanticAlignmentRunner(
            {"fake": {"model": "fake"}}, "fake", Path("prompts/semantic_alignment"), directory,
            client=FakeClient({}),
        )
        results, report = runner.run([package])
    assert results[0]["status"] == "failed"
    assert results[0]["errors"][0].startswith("unhandled:KeyError:")
    assert report["failed_count"] == 1
    assert report["protocol_success_rate"] == 0.0
