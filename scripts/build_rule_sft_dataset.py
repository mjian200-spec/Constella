from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from constella.rule_extraction.context_cache import ContextCache
from constella.rule_extraction.message_builder import MultimodalMessageBuilder


def main() -> None:
    parser = argparse.ArgumentParser(description="Build reviewed rule-extraction SFT data from annotations.")
    parser.add_argument("--annotations", required=True, help="Reviewed YAML annotation file.")
    parser.add_argument("--context-cache", required=True, help="Resolved context cache directory.")
    parser.add_argument("--model-output-cache", required=True, help="Model output cache directory.")
    parser.add_argument("--output", required=True, help="Generated YAML dataset path.")
    args = parser.parse_args()

    annotation_path = Path(args.annotations)
    annotation_data = yaml.safe_load(annotation_path.read_text(encoding="utf-8"))
    context_cache = ContextCache(args.context_cache)
    model_output_cache = Path(args.model_output_cache)
    message_builder = MultimodalMessageBuilder()
    samples = []

    for annotation in annotation_data["annotations"]:
        package_id = annotation["id"]
        cache_path = context_cache.path_for(package_id)
        raw_context = json.loads(cache_path.read_text(encoding="utf-8"))
        package = context_cache.load(package_id, raw_context["source_fingerprint"])
        if package is None:
            raise ValueError(f"Cannot load resolved context package: {package_id}")

        generation_path = model_output_cache / package_id / "generate.json"
        generation = json.loads(generation_path.read_text(encoding="utf-8"))
        prompt_id = generation["prompt_id"]
        route = prompt_id.removeprefix("rule_generator_routed__").replace("__", "+")
        input_data = {"text": message_builder.context_content(package)[0]["text"]}
        images = [
            {
                "unit_id": asset.unit.id,
                "type": asset.unit.type,
                "page": asset.unit.source.get("page"),
                "caption": asset.caption,
                "path": asset.resolved_path,
            }
            for asset in package.assets
            if asset.resolved_path
        ]
        if images:
            input_data["images"] = images

        samples.append({
            "id": package_id,
            "route": route,
            "difficulty_tags": annotation["difficulty_tags"],
            "construction_rationale": annotation["construction_rationale"],
            "source": {
                "context_cache": str(cache_path),
                "model_output_cache": str(generation_path.parent),
                "prompt_id": prompt_id,
                "prompt_version": generation["prompt_version"],
            },
            "input": input_data,
            "output": annotation["output"],
        })

    dataset = {
        "schema_version": 2,
        "dataset": annotation_data.get("dataset", "constella_rule_extraction_sft_latest_30"),
        "language": "zh-CN",
        "task": "从最新语义上下文包抽取规则DSL",
        "source_context_run": annotation_data["source_context_run"],
        "source_rule_run": annotation_data["source_rule_run"],
        "notes": annotation_data.get(
            "notes",
            "输入由当前MultimodalMessageBuilder从不可变语义上下文cache生成；输出为人工复核金标。",
        ),
        "source_issue_log": annotation_data.get("source_issue_log", []),
        "samples": samples,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        yaml.safe_dump(dataset, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
