#!/usr/bin/env python3
"""Create a reproducible, real-context stress manifest for rule extraction."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from constella.rule_extraction.resolver import DocumentGraphIndex, iter_packages, resolve_package


REQUIRED = {
    "context_000108", "context_000202", "context_000252", "context_000443", "context_000453",
    "context_000457", "context_000465", "context_000472", "context_000637", "context_000801", "context_000168",
}


def _text_length(resolved) -> int:
    return sum(len(str(unit.content or "")) for unit in resolved.core_units + resolved.support_units) + sum(len(str(asset.unit.content or "")) for asset in resolved.assets)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--context-output-dir", default="outputs/context_builder")
    parser.add_argument("--output", default="outputs/rule_extraction/stress_manifest.json")
    parser.add_argument("--count", type=int, default=40)
    args = parser.parse_args()
    root = Path(args.context_output_dir)
    index = DocumentGraphIndex.load(root / "document_graph.json")
    candidates = []
    for package in iter_packages(root / "context_packages.jsonl"):
        resolved = resolve_package(index, package)
        kinds = {asset.unit.type for asset in resolved.assets}
        text_length = _text_length(resolved)
        score = text_length + len(resolved.constraints) * 500 + len(kinds & {"figure", "table", "formula"}) * 1000 + len(package.get("attributes", {}).get("routing_evidence", [])) * 50
        candidates.append((package, resolved, kinds, text_length, score))
    selected: dict[str, set[str]] = {}
    by_id = {package["id"]: item for package, *item in candidates}
    for package_id in REQUIRED:
        if package_id in by_id:
            selected[package_id] = {"fixed_real_case"}
    for label, items in (("long_text_top10", sorted(candidates, key=lambda item: item[3], reverse=True)[:10]),
                         ("constraint_top10", sorted(candidates, key=lambda item: len(item[1].constraints), reverse=True)[:10])):
        for package, *_ in items:
            selected.setdefault(package["id"], set()).add(label)
    quotas = (("figure", 15), ("table", 8), ("formula", 5), ("plain_text", 8))
    for label, count in quotas:
        existing = sum(label in kinds if label != "plain_text" else not kinds for package, resolved, kinds, *_ in candidates if package["id"] in selected)
        for package, resolved, kinds, *_ in sorted(candidates, key=lambda item: item[4], reverse=True):
            matches = (label in kinds) if label != "plain_text" else not kinds
            if existing >= count:
                break
            if matches and package["id"] not in selected:
                selected[package["id"]] = {f"quota_{label}"}
                existing += 1
    for package, _, _, _, _ in sorted(candidates, key=lambda item: item[4], reverse=True):
        if len(selected) >= args.count:
            break
        selected.setdefault(package["id"], {"complexity_fill"})
    records = []
    for package, resolved, kinds, text_length, score in candidates:
        reasons = selected.get(package["id"])
        if reasons:
            records.append({"context_package_id": package["id"], "reasons": sorted(reasons), "asset_types": sorted(kinds),
                            "constraint_count": len(resolved.constraints), "resolved_text_chars": text_length, "complexity_score": score})
    records.sort(key=lambda item: item["context_package_id"])
    source_fingerprint = hashlib.sha256((root / "document_graph.json").read_bytes() + (root / "context_packages.jsonl").read_bytes()).hexdigest()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"version": 1, "source_fingerprint": source_fingerprint, "records": records}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(records)} real context packages to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
