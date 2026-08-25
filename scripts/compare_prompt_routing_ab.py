#!/usr/bin/env python3
"""Export machine metrics and a readable evidence bundle for prompt-routing A/B review."""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from constella.rule_extraction.message_builder import MultimodalMessageBuilder
from constella.rule_extraction.resolver import DocumentGraphIndex, iter_packages, resolve_package


def model_output(root: Path, package_id: str, phase: str) -> str | None:
    path = root / "cache" / "model_outputs" / package_id / f"{phase}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("output")


def states(root: Path) -> dict[str, dict]:
    connection = sqlite3.connect(root / "rule_extraction_state.sqlite3")
    connection.row_factory = sqlite3.Row
    result = {row["context_package_id"]: dict(row) for row in connection.execute(
        "SELECT context_package_id,status,failure_stage,failure_code,failure_reason FROM package_states"
    )}
    connection.close()
    return result


def latencies(root: Path) -> dict[str, float]:
    connection = sqlite3.connect(root / "rule_extraction_state.sqlite3")
    rows = connection.execute(
        "SELECT context_package_id,SUM(latency_seconds) FROM model_calls WHERE status='ok' GROUP BY context_package_id"
    ).fetchall()
    connection.close()
    return {str(package_id): float(value) for package_id, value in rows}


def call_counts(root: Path) -> dict[str, dict[str, int]]:
    connection = sqlite3.connect(root / "rule_extraction_state.sqlite3")
    rows = connection.execute(
        "SELECT context_package_id,phase,COUNT(*) FROM model_calls WHERE status='ok' GROUP BY context_package_id,phase"
    ).fetchall()
    connection.close()
    result: dict[str, dict[str, int]] = defaultdict(dict)
    for package_id, phase, count in rows:
        result[str(package_id)][str(phase)] = int(count)
    return dict(result)


def rule_count(root: Path, package_id: str) -> int:
    path = root / "rulesets" / f"{package_id}.json"
    if not path.is_file():
        return 0
    return len(json.loads(path.read_text(encoding="utf-8")).get("rules", []))


def summarize(records: list[dict], variant: str) -> dict:
    valid = [item for item in records if item[variant]["status"] in {"success", "no_rule"}]
    timings = [item[variant]["latency_seconds"] for item in records if item[variant]["latency_seconds"] is not None]
    return {
        "package_count": len(records),
        "parse_success_count": len(valid),
        "failed_count": len(records) - len(valid),
        "rule_count": sum(item[variant]["rule_count"] for item in records),
        "mean_rules_per_package": round(statistics.mean(item[variant]["rule_count"] for item in records), 3),
        "mean_model_latency_seconds": round(statistics.mean(timings), 3),
        "median_model_latency_seconds": round(statistics.median(timings), 3),
        "mean_final_chars": round(statistics.mean(len(item[variant]["final"] or "") for item in records), 3),
        "model_call_count": sum(sum(item[variant]["call_counts"].values()) for item in records),
        "reflection_repair_call_count": sum(item[variant]["call_counts"].get("reflect_repair", 0) for item in records),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--context-output-dir", default="outputs/context_builder")
    parser.add_argument("--manifest", default="outputs/prompt_routing_ab/manifest.json")
    parser.add_argument("--baseline-dir", default="outputs/prompt_routing_ab/baseline")
    parser.add_argument("--routed-dir", default="outputs/prompt_routing_ab/routed")
    parser.add_argument(
        "--routed-override-dir",
        help="Optional routed result directory whose packages override --routed-dir (for targeted regressions).",
    )
    parser.add_argument("--output-dir", default="outputs/prompt_routing_ab")
    args = parser.parse_args()

    context_dir, baseline, routed = map(Path, (args.context_output_dir, args.baseline_dir, args.routed_dir))
    routed_override = Path(args.routed_override_dir) if args.routed_override_dir else None
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    manifest_by_id = {item["context_package_id"]: item for item in manifest["records"]}
    wanted = set(manifest_by_id)
    raw_packages = {item["id"]: item for item in iter_packages(context_dir / "context_packages.jsonl") if item["id"] in wanted}
    index = DocumentGraphIndex.load(context_dir / "document_graph.json")
    builder = MultimodalMessageBuilder()
    state_a, state_b = states(baseline), states(routed)
    latency_a, latency_b = latencies(baseline), latencies(routed)
    calls_a, calls_b = call_counts(baseline), call_counts(routed)
    override_state = states(routed_override) if routed_override else {}
    override_latency = latencies(routed_override) if routed_override else {}
    override_calls = call_counts(routed_override) if routed_override else {}
    records = []
    for package_id in sorted(wanted):
        package = resolve_package(index, raw_packages[package_id])
        text_context = builder.context_content(package)[0]["text"]
        variants = {}
        routed_root = routed_override if package_id in override_state else routed
        routed_package_state = override_state.get(package_id, state_b.get(package_id))
        routed_package_latency = override_latency if package_id in override_state else latency_b
        routed_package_calls = override_calls if package_id in override_state else calls_b
        for name, root, state, latency, calls in (
            ("baseline", baseline, state_a, latency_a, calls_a),
            ("routed", routed_root, {package_id: routed_package_state}, routed_package_latency, routed_package_calls),
        ):
            current = state[package_id]
            variants[name] = {
                **current,
                "latency_seconds": round(latency.get(package_id), 3) if package_id in latency else None,
                "rule_count": rule_count(root, package_id),
                "call_counts": calls.get(package_id, {}),
                "draft": model_output(root, package_id, "generate"),
                "reflection": model_output(root, package_id, "reflect"),
                "final": model_output(root, package_id, "candidate"),
            }
        records.append({
            **manifest_by_id[package_id],
            "section_path": package.section_path,
            "context": text_context,
            **variants,
        })

    by_stratum: dict[str, list[dict]] = defaultdict(list)
    for item in records:
        by_stratum[item["stratum"]].append(item)
    metrics = {
        "sample_count": len(records),
        "baseline": summarize(records, "baseline"),
        "routed": summarize(records, "routed"),
        "by_stratum": {
            group: {"baseline": summarize(items, "baseline"), "routed": summarize(items, "routed")}
            for group, items in sorted(by_stratum.items())
        },
    }
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "comparison_metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (output_dir / "comparison_records.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    markdown = ["# 真实上下文包提示词路由 A/B 审核材料", "", "A=当前统一提示词；B=按数据形态路由提示词。", ""]
    for item in records:
        markdown.extend([
            f"## {item['context_package_id']}（{item['route']}）", "", "### 核心上下文", "", "```text",
            item["context"], "```", "", "### A 当前版", "", f"状态：{item['baseline']['status']}", "", "```text",
            item["baseline"]["final"] or item["baseline"]["draft"] or "", "```", "", "### B 路由版", "",
            f"状态：{item['routed']['status']}", "", "```text", item["routed"]["final"] or item["routed"]["draft"] or "", "```", "",
        ])
    (output_dir / "comparison_review_bundle.md").write_text("\n".join(markdown), encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
