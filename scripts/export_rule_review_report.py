#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Export the latest manual rule-review feedback as Markdown.")
    parser.add_argument("--context-output-dir", default="outputs/context_builder")
    parser.add_argument("--extraction-output-dir", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    context_dir = Path(args.context_output_dir)
    extraction_dir = Path(args.extraction_output_dir)
    graph = json.loads((context_dir / "document_graph.json").read_text(encoding="utf-8"))
    packages = {
        item["id"]: item
        for item in (json.loads(line) for line in (context_dir / "context_packages.jsonl").read_text(encoding="utf-8").splitlines())
    }
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    package_ids = [item["context_package_id"] for item in manifest["records"]]
    feedback: dict[str, dict] = {}
    for line in (extraction_dir / "manual_feedback.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            item = json.loads(line)
            feedback[item["context_package_id"]] = item

    inappropriate = [package_id for package_id in package_ids if feedback.get(package_id, {}).get("verdict") == "inappropriate"]
    appropriate = [package_id for package_id in package_ids if feedback.get(package_id, {}).get("verdict") == "appropriate"]
    lines = [
        "# Qwen3.5-27B 规则抽取人工验收报告",
        "",
        f"- 验收总数：{len(package_ids)}",
        f"- 合适：{len(appropriate)}",
        f"- 不合适：{len(inappropriate)}",
        "- 验收依据：逐包核对核心原文、关联资源、三阶段模型输出和最终 DSL。",
        "",
        "## 不合适项：核心原文与参考答案",
    ]
    units = graph["units"]
    for index, package_id in enumerate(inappropriate, start=1):
        package = packages[package_id]
        core = "\n\n".join(str(units[unit_id].get("content", "")) for unit_id in package.get("core_unit_ids", []))
        item = feedback[package_id]
        lines.extend([
            "",
            f"### {index}. {package_id}",
            "",
            "核心原文：",
            "",
            core,
            "",
            "发现的问题：",
            "",
            item.get("note", ""),
            "",
            "参考答案：",
            "",
            "```text",
            item.get("standard_result", ""),
            "```",
        ])
    lines.extend(["", "## 人工判定合适的上下文包", "", ", ".join(appropriate), ""])
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {output}: {len(appropriate)} appropriate, {len(inappropriate)} inappropriate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
