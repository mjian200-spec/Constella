from __future__ import annotations

import argparse
from pathlib import Path

import yaml


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge reviewed rule-extraction SFT YAML datasets.")
    parser.add_argument("inputs", nargs="+", help="Input dataset YAML files in target order.")
    parser.add_argument("--output", required=True, help="Merged dataset YAML path.")
    parser.add_argument("--dataset", default="constella_rule_extraction_sft_200")
    args = parser.parse_args()

    samples: list[dict] = []
    issue_log: list[dict] = []
    seen_ids: set[str] = set()
    sources: list[str] = []

    for raw_path in args.inputs:
        path = Path(raw_path)
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("samples"), list):
            raise ValueError(f"Dataset has no samples list: {path}")
        sources.append(str(path))
        for sample in data["samples"]:
            sample_id = sample.get("id")
            if not sample_id:
                raise ValueError(f"Sample without id in {path}")
            if sample_id in seen_ids:
                raise ValueError(f"Duplicate sample id: {sample_id}")
            seen_ids.add(sample_id)
            samples.append(sample)
        for issue in data.get("source_issue_log", []):
            if issue not in issue_log:
                issue_log.append(issue)

    output = {
        "schema_version": 2,
        "dataset": args.dataset,
        "language": "zh-CN",
        "task": "从真实焊接技术资料上下文包抽取规则DSL",
        "notes": (
            "200条人工逐项复核金标；input.text为模型文本上下文，"
            "input.images为需要随消息传入的原始图片，output仅含目标DSL。"
        ),
        "source_datasets": sources,
        "source_issue_log": issue_log,
        "samples": samples,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        yaml.safe_dump(output, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
