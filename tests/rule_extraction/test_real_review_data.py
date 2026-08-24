from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from constella.rule_extraction.review_server import RuleReviewData


ROOT = Path(__file__).resolve().parents[2]
CONTEXT_OUTPUT = ROOT / "outputs" / "context_builder"


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
