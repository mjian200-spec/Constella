#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from constella.concept_layer.article_discovery import ArticleDiscoveryRuntime, run_article_concept_discovery


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover concepts and structural relations directly from article context packages.")
    parser.add_argument("--context-output-dir", default="outputs/context_builder_full_20260826")
    parser.add_argument("--output-dir", default="outputs/article_concepts")
    parser.add_argument("--config-dir", default=str(ROOT / "configs" / "concept_layer"))
    parser.add_argument("--model-key", default="qwen3_8_27b")
    parser.add_argument("--workers", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--use-llm", action="store_true")
    args = parser.parse_args()
    config_dir = Path(args.config_dir)
    models = yaml.safe_load((config_dir / "models.yaml").read_text(encoding="utf-8"))["models"]
    pipeline = yaml.safe_load((config_dir / "pipeline.yaml").read_text(encoding="utf-8")) or {}
    runtime = ArticleDiscoveryRuntime(
        model_key=args.model_key, models=models,
        prompt_dir=ROOT / "prompts" / "concept_layer",
        max_workers=args.workers or int(models[args.model_key].get("max_concurrency", pipeline.get("max_workers", 16))),
        use_llm=args.use_llm,
    )
    report = run_article_concept_discovery(args.context_output_dir, args.output_dir, runtime, limit=args.limit)
    print(f"{report['package_count']} packages -> {report['concept_package_count']} concept packages -> {report['concept_count']} concepts, {report['relation_count']} relations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
