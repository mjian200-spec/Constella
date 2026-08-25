from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from constella.rule_extraction.review_server import RuleReviewData


ROOT = Path(__file__).resolve().parents[2]
CONTEXT_OUTPUT = ROOT / "outputs" / "context_builder"
FULL_CONTEXT_OUTPUT = ROOT / "outputs" / "context_builder_full_fresh"
FULL_EXTRACTION_OUTPUT = ROOT / "outputs" / "rule_extraction_full_v11_full_v2_20260825_run2"


@unittest.skipUnless((CONTEXT_OUTPUT / "document_graph.json").is_file(), "requires real Context Builder output")
class RealReviewDataTests(unittest.TestCase):
    def test_real_context_resources_and_manual_feedback_are_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = RuleReviewData(CONTEXT_OUTPUT, Path(directory))
            detail = data.package_detail("context_000472")
            self.assertEqual("context_000472", detail["resolved"]["id"])
            self.assertEqual("figure", detail["resolved"]["assets"][0]["unit"]["type"])
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
        self.assertEqual(0.0, summary["progress"]["estimated_remaining_seconds"])
        self.assertEqual(1773.0, summary["progress"]["elapsed_seconds"])

        largest = max(summary["packages"], key=lambda item: item["rule_count"])
        self.assertEqual("context_000168", largest["id"])
        self.assertEqual(101, largest["rule_count"])
        self.assertIn(largest["review_status"], {"unreviewed", "appropriate", "inappropriate"})
