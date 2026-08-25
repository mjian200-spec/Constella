#!/usr/bin/env python3
"""Build a deterministic, route-stratified manifest from real context packages."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from constella.rule_extraction.prompt_router import route_modalities, route_name
from constella.rule_extraction.resolver import DocumentGraphIndex, iter_packages, resolve_package


FIXED_CASES = {
    "text": ["context_000481"],
    "image": ["context_000391", "context_000505", "context_000472"],
    "table": ["context_000801", "context_000488", "context_000386"],
    "formula": ["context_000896", "context_000540", "context_000904", "context_000900"],
    "mixed": ["context_000786", "context_000037", "context_000069", "context_000243"],
}
QUOTAS = {"text": 8, "image": 8, "table": 8, "formula": 8, "mixed": 8}


def stratum(route: str) -> str:
    specialists = [item for item in route.split("+") if item != "text"]
    if not specialists:
        return "text"
    return specialists[0] if len(specialists) == 1 else "mixed"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--context-output-dir", default="outputs/context_builder")
    parser.add_argument("--output", default="outputs/prompt_routing_ab/manifest.json")
    args = parser.parse_args()

    context_dir = Path(args.context_output_dir)
    index = DocumentGraphIndex.load(context_dir / "document_graph.json")
    candidates: dict[str, list[dict]] = defaultdict(list)
    by_id: dict[str, dict] = {}
    for raw in iter_packages(context_dir / "context_packages.jsonl"):
        package = resolve_package(index, raw)
        route = route_name(route_modalities(package))
        text_chars = sum(len(str(unit.content or "")) for unit in package.core_units + package.support_units)
        asset_chars = sum(len(str(asset.unit.content or "")) for asset in package.assets)
        record = {
            "context_package_id": package.id,
            "route": route,
            "stratum": stratum(route),
            "asset_types": sorted({asset.unit.type for asset in package.assets}),
            "constraint_count": len(package.constraints),
            "resolved_text_chars": text_chars + asset_chars,
            "complexity_score": text_chars + asset_chars + 500 * len(package.constraints) + 1000 * len(package.assets),
        }
        candidates[record["stratum"]].append(record)
        by_id[package.id] = record

    selected: list[dict] = []
    selected_ids: set[str] = set()
    for group, quota in QUOTAS.items():
        for package_id in FIXED_CASES[group]:
            record = by_id.get(package_id)
            if record and record["stratum"] == group and package_id not in selected_ids:
                selected.append({**record, "selection_reason": "fixed_semantic_case"})
                selected_ids.add(package_id)
        ranked = sorted(candidates[group], key=lambda item: (-item["complexity_score"], item["context_package_id"]))
        while sum(item["stratum"] == group for item in selected) < quota:
            record = next(item for item in ranked if item["context_package_id"] not in selected_ids)
            selected.append({**record, "selection_reason": "route_complexity"})
            selected_ids.add(record["context_package_id"])

    selected.sort(key=lambda item: item["context_package_id"])
    source_fingerprint = hashlib.sha256(
        (context_dir / "document_graph.json").read_bytes()
        + (context_dir / "context_packages.jsonl").read_bytes()
    ).hexdigest()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({
        "version": 1,
        "purpose": "baseline_vs_modality_routed_prompt_ab",
        "source_fingerprint": source_fingerprint,
        "quotas": QUOTAS,
        "records": selected,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(selected)} real packages to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
