from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from constella.rule_extraction.models import PackageProcessingResult
from constella.rule_extraction.generator import RuleGenerator
from constella.rule_extraction.pipeline import _TerminalProgress
from constella.rule_extraction.state_store import StateStore


class StateStoreProgressTests(unittest.TestCase):
    def test_resumed_retry_is_queued_and_progress_counts_only_selected_packages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            store = StateStore(path)
            run_id = store.create_run("fingerprint", "model", {})
            store.set_result(PackageProcessingResult(
                "old_success", "success", input_fingerprint="fingerprint", run_id=run_id,
            ))
            store.set_result(PackageProcessingResult(
                "retry_me", "failed", failure_stage="generate", failure_code="timeout",
                input_fingerprint="fingerprint", run_id=run_id,
            ))

            store.queue_packages(run_id, ["retry_me", "new_package"], "fingerprint")
            progress = _TerminalProgress(path, run_id, {"retry_me", "new_package"})

            self.assertEqual({"queued": 2}, progress._counts())
            self.assertEqual("queued", store.package_state(run_id, "retry_me")["status"])
            self.assertIsNone(store.package_state(run_id, "retry_me")["failure_code"])
            store.close()

    def test_reflector_repairs_malformed_protocol_output_before_it_is_cached(self) -> None:
        generator = object.__new__(RuleGenerator)
        generator.reflector_prompt = {"id": "reflector", "version": 3, "system": "test"}
        generator.repair_prompt = {"id": "repair", "version": 1, "system": "test"}
        calls: list[str] = []
        cached: list[str] = []
        generator._call = lambda prompt, content, package_id, phase: calls.append(phase) or "NO_CHANGES"
        generator.output_sink = lambda **event: cached.append(event["output"])
        draft = "规则组1\nC: 无\nR: 输入|状态 —[导致]→ 输出|状态"

        result = generator.ensure_valid_reflection(
            [], "context_test", draft, "审核发现：\nNO_CHANGES",
        )

        self.assertEqual("NO_CHANGES", result)
        self.assertEqual(["reflect_repair"], calls)
        self.assertEqual(["NO_CHANGES"], cached)


if __name__ == "__main__":
    unittest.main()
