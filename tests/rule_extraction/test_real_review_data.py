from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from constella.rule_extraction.review_server import RuleReviewData


ROOT = Path(__file__).resolve().parents[2]
CONTEXT_OUTPUT = ROOT / "outputs" / "context_builder"
FULL_CONTEXT_OUTPUT = ROOT / "outputs" / "context_builder_full_fresh"
FULL_EXTRACTION_OUTPUT = ROOT / "outputs" / "rule_extraction_full_v11_full_v2_20260825_run2"
ROUTED_EXTRACTION_OUTPUT = ROOT / "outputs" / "prompt_routing_ab" / "routed"


@unittest.skipUnless((CONTEXT_OUTPUT / "document_graph.json").is_file(), "requires real Context Builder output")
class RealReviewDataTests(unittest.TestCase):
    def test_real_context_resources_and_manual_feedback_are_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = RuleReviewData(CONTEXT_OUTPUT, Path(directory))
            detail = data.package_detail("context_000472")
            self.assertEqual("context_000472", detail["resolved"]["id"])
            self.assertEqual("figure", detail["resolved"]["assets"][0]["unit"]["type"])
            self.assertEqual(["text", "image"], detail["route"]["expected"]["modalities"])
            self.assertEqual("pending", detail["route"]["status"])

            output_path = Path(directory) / "cache" / "model_outputs" / "context_000472" / "generate.json"
            output_path.parent.mkdir(parents=True)
            output_path.write_text(json.dumps({
                "input_fingerprint": "real-package-test",
                "prompt_id": "rule_generator_routed__text__image",
                "prompt_version": "base@4+text@1+image@3",
                "output": "规则组1\nC: 无\nR: A|1 —[导致]→ B|2",
            }), encoding="utf-8")
            routed_detail = data.package_detail("context_000472")
            self.assertEqual("matched", routed_detail["route"]["status"])
            self.assertEqual(
                "rule_generator_routed__text__image",
                routed_detail["model_outputs"]["generate"]["prompt_id"],
            )
            saved = data.save_feedback({
                "context_package_id": "context_000472", "verdict": "inappropriate",
                "standard_result": "规则组1\nC: 气瓶压力|低于1MPa\nR: 气瓶压力|低于1MPa —[导致]→ 含水量|增加",
                "note": "真实上下文包反馈测试",
            })
            self.assertEqual("inappropriate", saved["verdict"])
            self.assertEqual(saved, data.feedback()["context_000472"])


@unittest.skipUnless(
    (FULL_CONTEXT_OUTPUT / "document_graph.json").is_file()
    and (FULL_EXTRACTION_OUTPUT / "rule_extraction_state.sqlite3").is_file(),
    "requires the current full extraction output",
)
class FullResultReviewDataTests(unittest.TestCase):
    def test_summary_exposes_completed_result_and_review_dimensions(self) -> None:
        summary = RuleReviewData(FULL_CONTEXT_OUTPUT, FULL_EXTRACTION_OUTPUT).summary()

        self.assertEqual("completed", summary["run"]["status"])
        self.assertEqual(911, summary["package_count"])
        self.assertEqual(7188, summary["result_stats"]["total_rules"])
        self.assertEqual(47, summary["result_stats"]["over_20"])
        self.assertEqual(101, summary["result_stats"]["max_rules"])
        self.assertEqual(911, sum(summary["feedback_counts"].values()))
        self.assertEqual(911, sum(summary["route_counts"].values()))
        self.assertEqual("legacy_uniform", summary["run"]["extraction_mode"])
        self.assertEqual(0.0, summary["progress"]["estimated_remaining_seconds"])
        self.assertEqual(1773.0, summary["progress"]["elapsed_seconds"])

        largest = max(summary["packages"], key=lambda item: item["rule_count"])
        self.assertEqual("context_000168", largest["id"])
        self.assertEqual(101, largest["rule_count"])
        self.assertIn(largest["review_status"], {"unreviewed", "appropriate", "inappropriate"})


@unittest.skipUnless(
    (CONTEXT_OUTPUT / "document_graph.json").is_file()
    and (ROUTED_EXTRACTION_OUTPUT / "rule_extraction_state.sqlite3").is_file(),
    "requires the real routed A/B extraction output",
)
class RoutedResultReviewDataTests(unittest.TestCase):
    def test_real_routed_run_exposes_only_selected_packages_and_prompt_route(self) -> None:
        data = RuleReviewData(CONTEXT_OUTPUT, ROUTED_EXTRACTION_OUTPUT)
        summary = data.summary()
        detail = data.package_detail("context_000391")

        self.assertEqual(40, summary["package_count"])
        self.assertEqual("structure_routed", summary["run"]["extraction_mode"])
        self.assertEqual(40, sum(summary["route_counts"].values()))
        self.assertEqual(["text", "image"], detail["route"]["expected"]["modalities"])
        self.assertEqual(["image"], detail["route"]["actual"]["modalities"])
        self.assertEqual("mismatch", detail["route"]["status"])
