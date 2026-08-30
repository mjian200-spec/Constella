from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path

import yaml

from constella.rule_extraction.parser import parse_final_expression


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a reviewed rule-extraction SFT dataset.")
    parser.add_argument("dataset", help="Dataset YAML path.")
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--min-rationale-chars", type=int, default=80)
    args = parser.parse_args()

    path = Path(args.dataset)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    samples = data.get("samples") if isinstance(data, dict) else None
    if not isinstance(samples, list):
        raise ValueError("Dataset must contain a samples list")
    if args.expected_count is not None and len(samples) != args.expected_count:
        raise ValueError(f"Expected {args.expected_count} samples, got {len(samples)}")

    errors: list[str] = []
    ids: set[str] = set()
    route_counts: Counter[str] = Counter()
    rule_count = 0
    image_count = 0

    for sample in samples:
        sample_id = str(sample.get("id") or "")
        if not sample_id:
            errors.append("sample without id")
            continue
        if sample_id in ids:
            errors.append(f"{sample_id}: duplicate sample id")
        ids.add(sample_id)
        route_counts[str(sample.get("route") or "unknown")] += 1

        rationale = re.sub(r"\s+", "", str(sample.get("construction_rationale") or ""))
        if len(rationale) < args.min_rationale_chars:
            errors.append(
                f"{sample_id}: rationale has {len(rationale)} non-space chars; "
                f"minimum is {args.min_rationale_chars}"
            )

        input_data = sample.get("input")
        if not isinstance(input_data, dict) or not str(input_data.get("text") or "").strip():
            errors.append(f"{sample_id}: missing input.text")
            continue
        images = input_data.get("images", [])
        if not isinstance(images, list):
            errors.append(f"{sample_id}: input.images must be a list")
            images = []
        for image in images:
            image_count += 1
            image_path = Path(str(image.get("path") or ""))
            if not image_path.is_file():
                errors.append(f"{sample_id}: missing image {image_path}")
        if "image" in str(sample.get("route") or "") and not images:
            errors.append(f"{sample_id}: image route has no input.images")

        output = str(sample.get("output") or "")
        if re.search(
            r"^\s*(?:无规则|没有(?:可抽取)?规则|no[_ -]?rule)\s*$",
            output,
            re.I | re.M,
        ):
            errors.append(f"{sample_id}: no-rule output is not allowed")
        for line in output.splitlines():
            if line.strip().startswith("R:") and not re.search(r"(?:→|->|⇒|⟶)", line):
                errors.append(f"{sample_id}: R line has no arrow: {line.strip()}")
        try:
            ruleset = parse_final_expression(
                output,
                sample_id,
                prompt_id="human_gold",
                prompt_version="1",
                model="human",
            )
        except Exception as exc:  # parser error must retain the sample id
            errors.append(f"{sample_id}: parse failed: {exc}")
            continue
        if not ruleset.rules:
            errors.append(f"{sample_id}: parsed zero rules")
        rule_count += len(ruleset.rules)
        for rule in ruleset.rules:
            if len(rule.consequents) != 1:
                errors.append(f"{sample_id}: rule has {len(rule.consequents)} consequents: {rule.raw_expression}")
            conditions = {(item.object, item.raw_state) for item in rule.conditions}
            antecedents = {(item.object, item.raw_state) for item in rule.antecedents}
            overlap = conditions & antecedents
            if overlap:
                errors.append(f"{sample_id}: identical C/R input states {sorted(overlap)}")

    if errors:
        raise ValueError("Dataset validation failed:\n- " + "\n- ".join(errors))
    print(f"validated_samples={len(samples)}")
    print(f"validated_rules={rule_count}")
    print(f"validated_images={image_count}")
    print("routes=" + ",".join(f"{key}:{value}" for key, value in sorted(route_counts.items())))


if __name__ == "__main__":
    main()
