from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from .llm_client import LLMClient
from .models import ContextPackage, DocumentGraph, PipelineRuntime


ROUTE_CODES = {
    "C": "concept",
    "R": "rule",
    "B": "concept_and_rule",
    "N": "noise",
}


def route_context_packages(
    graph: DocumentGraph, packages: list[ContextPackage], runtime: PipelineRuntime,
    *, client: Any | None = None,
) -> None:
    """Classify completed packages with one constrained, minimal label each."""
    if not runtime.use_package_router:
        for package in packages:
            package.attributes.setdefault("package_role", {"status": "model_not_run", "label": None})
        runtime.record(stage="package_role_classification", candidates=len(packages), processed=0, status="model_not_run")
        return
    prompt = _load_prompt(runtime)
    model_client = client or LLMClient(runtime.model_config, event_sink=runtime.record)
    cache_dir = (runtime.output_dir or Path("outputs/context_builder")) / "cache" / "package_routing"
    cache_dir.mkdir(parents=True, exist_ok=True)

    def classify(package):
        return package.id, classify_package(graph, package, runtime, prompt, model_client, cache_dir)

    by_id = {package.id: package for package in packages}
    failures = 0
    with ThreadPoolExecutor(max_workers=runtime.package_workers, thread_name_prefix="package-routing") as pool:
        futures = {pool.submit(classify, package): package.id for package in packages}
        for future in as_completed(futures):
            try:
                package_id, result = future.result()
                by_id[package_id].attributes["package_role"] = result
            except Exception as error:
                failures += 1
                package_id = futures[future]
                by_id[package_id].attributes["package_role"] = {
                    "status": "failed", "label": None,
                    "error_type": type(error).__name__, "reason": str(error),
                }
    counts = Counter(
        package.attributes.get("package_role", {}).get("label") or "failed"
        for package in packages
    )
    runtime.record(
        stage="package_role_classification", candidates=len(packages),
        processed=len(packages) - failures, failures=failures, labels=dict(sorted(counts.items())),
    )


def classify_package(graph, package, runtime, prompt, client, cache_dir) -> dict[str, Any]:
    payload = package_text_payload(graph, package)
    fingerprint = hashlib.sha256(json.dumps({
        "prompt": [prompt["id"], str(prompt["version"])],
        "model": runtime.model_config[runtime.package_router_model_key]["model"],
        "payload": payload,
    }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    cache_path = cache_dir / f"{package.id}.json"
    cached = _read_cache(cache_path, fingerprint)
    if cached is not None:
        return cached
    response = client.complete(
        runtime.package_router_model_key,
        [{"role": "system", "content": prompt["system"]}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        prompt_id=prompt["id"], prompt_version=str(prompt["version"]),
        input_unit_ids=payload["unit_ids"], max_tokens=4,
        structured_outputs={"choice": list(ROUTE_CODES)},
    )
    code = str(response["choices"][0]["message"]["content"]).strip()
    if code not in ROUTE_CODES:
        raise ValueError(f"unsupported constrained package role: {code!r}")
    label = ROUTE_CODES[code]
    result = {
        "status": "ok", "code": code, "label": label,
        "is_concept_package": label in {"concept", "concept_and_rule"},
        "is_rule_package": label in {"rule", "concept_and_rule"},
        "is_useless": label == "noise", "prompt_id": prompt["id"],
        "prompt_version": str(prompt["version"]),
        "configured_model": runtime.model_config[runtime.package_router_model_key]["model"],
        "served_model": response.get("model"), "fingerprint": fingerprint,
    }
    _write_cache(cache_path, result)
    return result


def package_text_payload(graph: DocumentGraph, package: ContextPackage) -> dict[str, Any]:
    ids = list(dict.fromkeys(package.core_unit_ids + package.support_unit_ids + package.asset_part_ids))
    units = []
    for unit_id in ids:
        unit = graph.units.get(unit_id)
        if unit is None:
            continue
        understanding = unit.attributes.get("resource_understanding") or {}
        units.append({
            "unit_id": unit.id, "type": unit.type, "content": unit.content,
            "caption": unit.attributes.get("caption"),
            "resource_description": understanding.get("description"),
            "formula_summary": understanding.get("summary"),
            "formula_symbols": understanding.get("symbols", []),
        })
    return {
        "package_id": package.id,
        "section_path": package.attributes.get("section_path", []),
        "unit_ids": [unit["unit_id"] for unit in units], "units": units,
    }


def _load_prompt(runtime: PipelineRuntime) -> dict[str, Any]:
    path = runtime.config_dir.parents[1] / "prompts" / "context_builder" / "package_role_classifier_v1.yaml"
    prompt = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not {"id", "version", "system"} <= set(prompt or {}):
        raise ValueError(f"Invalid package role prompt: {path}")
    return prompt


def _read_cache(path: Path, fingerprint: str) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if value.get("fingerprint") == fingerprint else None
    except (OSError, ValueError, TypeError):
        return None


def _write_cache(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
