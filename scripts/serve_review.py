#!/usr/bin/env python3
"""Serve the local Context Builder review page."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from constella.context_builder.review_server import serve_review


def main() -> int:
    parser = argparse.ArgumentParser(description="Open Context Builder outputs in a local review page.")
    parser.add_argument("--output-dir", default=str(ROOT / "outputs" / "context_builder"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    args = parser.parse_args()
    serve_review(ROOT / "web" / "review", args.output_dir, args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
