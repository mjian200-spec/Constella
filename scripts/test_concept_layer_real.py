#!/usr/bin/env python3
"""Run the project's default real-data Concept Layer test and strict evaluation."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "concept_layer" / "evaluation.yaml"


def main() -> int:
    defaults = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    parser = argparse.ArgumentParser(description="Run Concept Layer validation on real rules (default: 100).")
    parser.add_argument("--limit", type=int, default=int(defaults["real_rule_count"]))
    parser.add_argument("--workers", type=int, default=int(defaults["max_workers"]))
    parser.add_argument("--model-key", default=defaults["model_key"])
    parser.add_argument("--context-output-dir", default=defaults["context_output_dir"])
    parser.add_argument("--rule-output-dir", default=defaults["rule_output_dir"])
    parser.add_argument("--output-dir", default=defaults["output_dir"])
    args = parser.parse_args()
    build = [
        sys.executable, str(ROOT / "scripts" / "build_concept_layer.py"),
        "--context-output-dir", args.context_output_dir,
        "--rule-output-dir", args.rule_output_dir,
        "--output-dir", args.output_dir,
        "--limit", str(args.limit), "--workers", str(max(1, args.workers)),
        "--model-key", args.model_key, "--use-llm",
    ]
    completed = subprocess.run(build, cwd=ROOT, check=False)
    if completed.returncode:
        return completed.returncode
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "evaluate_concept_layer.py"), args.output_dir],
        cwd=ROOT, check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
