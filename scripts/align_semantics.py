#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from constella.semantic_alignment import (  # noqa: E402
    MemorySnapshot,
    PackageTier,
    SemanticAlignmentRunner,
    SemanticPackageBuilder,
    assemble_semantics,
    load_alignment_inputs,
)
from constella.semantic_alignment.assembly import write_json, write_jsonl  # noqa: E402
from constella.semantic_alignment.evaluation import artifact_metrics  # noqa: E402
from constella.semantic_alignment.models import TIER_ORDER  # noqa: E402


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _output_suffix(args: argparse.Namespace) -> str:
    if args.object_limit is not None:
        return "_trial"
    if args.max_tier != PackageTier.H3:
        return f"_through_{str(args.max_tier).lower()}"
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compile traceable object/state semantics with a frozen reviewed concept memory.",
    )
    parser.add_argument("--rule-output-dir", default="outputs/rule_extraction_full_20260829")
    parser.add_argument("--concept-output-dir", default="outputs/article_concepts_full_20260829")
    parser.add_argument("--context-output-dir", default="outputs/context_builder_semantic_qwen38_27b_20260829")
    parser.add_argument("--output-dir", default="outputs/semantic_alignment_content_v2")
    parser.add_argument("--reviewed-memory", help="Optional JSONL of externally approved concepts, types, or aliases.")
    parser.add_argument("--config-dir", default=str(ROOT / "configs" / "concept_layer"))
    parser.add_argument("--model-key", default="qwen3_8_27b")
    parser.add_argument(
        "--max-tier", choices=[str(value) for value in PackageTier], default=PackageTier.H1,
        help="Stop at this confidence tier. Default H1 enforces review before complex packages.",
    )
    parser.add_argument("--object-limit", type=int, help="Limit selected LLM packages; writes trial-suffixed outputs.")
    parser.add_argument("--proposal-threshold", type=int, default=5)
    parser.add_argument("--candidates-per-object", type=int, default=6)
    parser.add_argument("--objects-per-package", type=int, default=16)
    parser.add_argument("--max-package-chars", type=int, default=40_000)
    parser.add_argument("--workers", type=int)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Build and score packages without calling the model.")
    args = parser.parse_args()

    inputs = load_alignment_inputs(args.rule_output_dir, args.concept_output_dir, args.context_output_dir)
    reviewed_memory = _read_jsonl(Path(args.reviewed_memory)) if args.reviewed_memory else []
    memory = MemorySnapshot.build(inputs.concepts, inputs.relations, reviewed_memory)
    builder = SemanticPackageBuilder(inputs, memory=memory)
    packages = builder.object_alignment_packages(
        candidates_per_object=args.candidates_per_object,
        objects_per_package=args.objects_per_package,
        max_package_chars=args.max_package_chars,
    )
    eligible = [
        package for package in packages
        if TIER_ORDER[PackageTier(package["tier"])] <= TIER_ORDER[PackageTier(args.max_tier)]
    ]
    selected_packages = eligible[:args.object_limit] if args.object_limit is not None else eligible
    dry_report = {
        "schema_version": "semantic_alignment.v2",
        "rule_count": len(inputs.rules),
        "source_state_count": len(builder.state_rows),
        "source_occurrence_count": builder.source_occurrence_count,
        "normalized_object_count": len(builder.object_rows),
        "source_raw_object_variant_count": len({row["raw_object"] for row in builder.state_rows.values()}),
        "memory_version": memory.version,
        "registry_concept_count": len(memory.concepts),
        "approved_memory_count": memory.approved_memory_count,
        "package_parameters": {
            "candidates_per_object": args.candidates_per_object,
            "objects_per_package": args.objects_per_package,
            "max_package_chars": args.max_package_chars,
        },
        "max_tier": str(args.max_tier),
        "selected_llm_package_count": len(selected_packages),
        **builder.package_report(packages),
        "input_manifest": inputs.input_manifest,
    }
    if args.dry_run:
        print(json.dumps(dry_report, ensure_ascii=False, indent=2))
        return 0

    output_dir = Path(args.output_dir)
    if selected_packages:
        config = yaml.safe_load((Path(args.config_dir) / "models.yaml").read_text(encoding="utf-8"))
        models = config["models"]
        workers = args.workers or int(models[args.model_key].get("max_concurrency", 1))
        runner = SemanticAlignmentRunner(
            models, args.model_key, ROOT / "prompts" / "semantic_alignment", output_dir,
            workers=workers,
        )
        results, run_report = runner.run(
            selected_packages, max_tier=args.max_tier, refresh=args.refresh,
        )
    else:
        results = []
        run_report = {
            "package_type": "object_alignment",
            "selected_package_count": 0,
            "eligible_package_count": 0,
            "success_count": 0,
            "failed_count": 0,
            "cached_count": 0,
            "protocol_success_rate": 1.0,
            "decision_coverage_rate": 1.0,
            "max_tier": str(args.max_tier),
            "tier_reports": [],
            "elapsed_seconds": 0.0,
        }
    selected_object_ids = builder.selected_object_ids(
        selected_packages,
        include_mechanical=args.object_limit is None,
        max_tier=args.max_tier,
    )
    object_rows, state_rows, proposal_rows, coverage_rows, assembly_report = assemble_semantics(
        builder,
        selected_packages,
        results,
        selected_object_ids=selected_object_ids,
        proposal_threshold=args.proposal_threshold,
    )
    suffix = _output_suffix(args)
    write_jsonl(output_dir / f"object_semantics{suffix}.jsonl", object_rows)
    write_jsonl(output_dir / f"state_semantics{suffix}.jsonl", state_rows)
    write_jsonl(output_dir / f"alignment_proposals{suffix}.jsonl", proposal_rows)
    write_jsonl(output_dir / f"state_coverage{suffix}.jsonl", coverage_rows)
    final_report = {
        **dry_report,
        "run_mode": "trial" if args.object_limit is not None else "staged" if suffix else "full",
        "outputs_suffix": suffix,
        "runner": run_report,
        "assembly": assembly_report,
        "evaluation": artifact_metrics(object_rows, state_rows, proposal_rows, coverage_rows),
    }
    write_json(output_dir / f"alignment_report{suffix}.json", final_report)
    print(json.dumps(final_report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
