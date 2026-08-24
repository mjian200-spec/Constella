#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from constella.rule_extraction.review_server import serve_rule_review


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the live Rule Extraction review page.")
    parser.add_argument("--context-output-dir", default=str(ROOT / "outputs" / "context_builder"))
    parser.add_argument("--extraction-output-dir", default=str(ROOT / "outputs" / "rule_extraction_full"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--manifest", help="Only show context package IDs in a stress manifest.")
    args = parser.parse_args()
    package_ids = None
    if args.manifest:
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        package_ids = {str(item["context_package_id"]) for item in manifest.get("records", [])}
    serve_rule_review(ROOT / "web" / "rule_review", args.context_output_dir, args.extraction_output_dir, args.host, args.port, package_ids)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
