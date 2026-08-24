from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "outputs" / "context_builder"


@unittest.skipUnless((OUTPUT / "document_graph.json").is_file(), "requires real Context Builder output")
class RealStressManifestTests(unittest.TestCase):
    def test_real_manifest_covers_required_asset_types_and_cases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stress.json"
            completed = subprocess.run(
                [sys.executable, "scripts/inspect_rule_extraction.py", "--context-output-dir", str(OUTPUT), "--output", str(path)],
                cwd=ROOT, check=True, text=True, capture_output=True,
            )
            manifest = json.loads(path.read_text(encoding="utf-8"))
        records = manifest["records"]
        self.assertGreaterEqual(len(records), 40)
        ids = {item["context_package_id"] for item in records}
        self.assertTrue({"context_000168", "context_000252", "context_000801"}.issubset(ids))
        self.assertGreaterEqual(sum("figure" in item["asset_types"] for item in records), 15)
        self.assertGreaterEqual(sum("table" in item["asset_types"] for item in records), 8)
        self.assertGreaterEqual(sum("formula" in item["asset_types"] for item in records), 5)
