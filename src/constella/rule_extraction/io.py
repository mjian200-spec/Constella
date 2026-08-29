from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import PackageProcessingResult, StructuredRuleSet


def write_ruleset(directory: str | Path, ruleset: StructuredRuleSet) -> Path:
    target = Path(directory) / "rulesets"
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{ruleset.context_package_id}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(asdict(ruleset), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


def export_outputs(output_dir: str | Path, packages: list[dict[str, Any]], results: list[PackageProcessingResult], report: dict[str, Any]) -> None:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    results_by_id = {item.context_package_id: item for item in results}
    processed: list[dict[str, Any]] = []
    rules: list[dict[str, Any]] = []
    for package in packages:
        result = results_by_id.get(package["id"])
        if result is None:
            continue
        row = dict(package)
        row.update({
            "extraction_status": result.status, "rule_ids": result.rule_ids,
            "failure_stage": result.failure_stage, "failure_code": result.failure_code,
            "failure_reason": result.failure_reason, "run_id": result.run_id,
        })
        processed.append(row)
        if result.status == "success":
            rule_path = directory / "rulesets" / f"{package['id']}.json"
            if rule_path.is_file():
                ruleset = json.loads(rule_path.read_text(encoding="utf-8"))
                rules.extend(ruleset.get("rules", []))
    _write_jsonl(directory / "processed_context_packages.jsonl", processed)
    _write_jsonl(directory / "structured_rules.jsonl", rules)
    (directory / "rule_extraction_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    temporary.replace(path)
