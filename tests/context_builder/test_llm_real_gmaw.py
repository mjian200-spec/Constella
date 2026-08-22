from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
import unittest

from constella.context_builder.pipeline import load_runtime, run_context_builder


ROOT = Path(__file__).resolve().parents[2]
INPUT_PATH = ROOT / "GMAW/hybrid_ocr/GMAW(OCR)_content_list.json"


@unittest.skipUnless(
    INPUT_PATH.exists() and os.environ.get("CONSTELLA_TEST_LLM") == "1",
    "requires real GMAW input and explicit CONSTELLA_TEST_LLM=1",
)
class RealGMAWLLMTests(unittest.TestCase):
    """An opt-in strong test against the local Qwen service and real OCR blocks."""

    @staticmethod
    def raw_content(block: dict) -> str:
        if block["type"] == "list":
            content = "\n".join(block.get("list_items", []))
        else:
            content = block.get("text") or block.get("equation") or ""
        return re.sub(r"(?<=\S)[ \t]*\n[ \t]*(?=\S)", "", content).strip()

    def test_one_real_batch_is_auditable_and_candidate_bounded(self):
        raw_blocks = json.loads(INPUT_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            graph = run_context_builder(
                str(INPUT_PATH), str(Path(directory) / "context_builder"),
                load_runtime(ROOT / "configs/context_builder", use_llm=True, llm_max_batches=1),
            )
        calls = [event for event in graph.metadata["model_calls"] if event["task"] == "completion"]
        self.assertEqual(1, len(calls))
        call = calls[0]
        self.assertEqual("ok", call["status"])
        self.assertEqual("route_classifier", call["prompt_id"])
        self.assertEqual(2, call["prompt_version"])
        self.assertGreaterEqual(len(call["input_unit_ids"]), 1)
        self.assertLessEqual(len(call["input_unit_ids"]), 12)
        for unit_id in call["input_unit_ids"]:
            unit = graph.units[unit_id]
            source_index = int(unit.source.original_block_id.rsplit(":", 1)[1])
            self.assertEqual(self.raw_content(raw_blocks[source_index]), unit.content)
            decision = unit.attributes["llm_route"]
            self.assertEqual("ok", decision["status"])
            self.assertIn(decision["selected_role"], {"rule", "structured_candidate", "support", "unknown"})
            llm_candidates = [item for item in unit.attributes["route_candidates"] if item.get("source") == "llm"]
            self.assertTrue(all(item["role"] in {"rule", "structured_candidate", "support"} for item in llm_candidates))
