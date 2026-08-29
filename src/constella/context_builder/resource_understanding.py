from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from constella.rule_extraction.image_adapter import ImageAdapter, ImageAdapterError

from .cleaning import ordered_units
from .llm_client import LLMClient
from .models import DocumentGraph, PipelineRuntime


def understand_document_resources(
    graph: DocumentGraph, runtime: PipelineRuntime, *, client: Any | None = None,
) -> None:
    """Textualize figures/tables and resolve formula symbols before packaging."""
    resources = [
        unit for unit in graph.units.values()
        if unit.type in {"figure", "table", "formula"}
    ]
    if runtime.resource_max_items is not None:
        resources = resources[:max(0, runtime.resource_max_items)]
    if not runtime.use_resource_llm:
        runtime.record(
            stage="resource_understanding", candidates=len(resources), processed=0,
            useful_assets=0, status="model_not_run",
        )
        return

    prompts = _load_prompts(runtime)
    model_client = client or LLMClient(runtime.model_config, event_sink=runtime.record)
    cache_dir = (runtime.output_dir or Path("outputs/context_builder")) / "cache" / "resource_understanding"
    cache_dir.mkdir(parents=True, exist_ok=True)
    positions = {unit_id: index for index, unit_id in enumerate(ordered_units(graph))}
    failures = 0

    def process(unit):
        if unit.type == "formula":
            return unit.id, _understand_formula(graph, unit.id, positions, runtime, prompts["formula"], model_client, cache_dir)
        return unit.id, _understand_asset(graph, unit.id, runtime, prompts["asset"], model_client, cache_dir)

    with ThreadPoolExecutor(max_workers=runtime.resource_workers, thread_name_prefix="resource-understanding") as pool:
        futures = {pool.submit(process, unit): unit.id for unit in resources}
        for future in as_completed(futures):
            try:
                unit_id, result = future.result()
                graph.units[unit_id].attributes["resource_understanding"] = result
            except Exception as error:
                failures += 1
                unit_id = futures[future]
                graph.units[unit_id].attributes["resource_understanding"] = {
                    "status": "failed", "error_type": type(error).__name__, "reason": str(error),
                }
    understood = [
        unit.attributes.get("resource_understanding", {})
        for unit in resources
    ]
    runtime.record(
        stage="resource_understanding", candidates=len(resources),
        processed=sum(item.get("status") == "ok" for item in understood), failures=failures,
        useful_assets=sum(bool(item.get("useful")) for item in understood),
    )


def _load_prompts(runtime: PipelineRuntime) -> dict[str, dict[str, Any]]:
    prompt_dir = runtime.config_dir.parents[1] / "prompts" / "context_builder"
    paths = {
        "asset": prompt_dir / "resource_textualizer_v1.yaml",
        "formula": prompt_dir / "formula_symbol_resolver_v1.yaml",
    }
    prompts = {key: yaml.safe_load(path.read_text(encoding="utf-8")) for key, path in paths.items()}
    for key, value in prompts.items():
        if not {"id", "version", "system"} <= set(value or {}):
            raise ValueError(f"Invalid {key} understanding prompt: {paths[key]}")
    return prompts


def _understand_asset(graph, unit_id, runtime, prompt, client, cache_dir) -> dict[str, Any]:
    unit = graph.units[unit_id]
    path = _resolve_asset_path(graph, unit)
    payload = {
        "unit_id": unit.id,
        "type": unit.type,
        "caption": unit.attributes.get("caption"),
        "source_text": unit.content,
        "table_body": unit.attributes.get("table_body") if unit.type == "table" else None,
    }
    fingerprint = _fingerprint(prompt, runtime, payload, path)
    cached = _read_cache(cache_dir / f"{unit.id}.json", fingerprint)
    if cached is not None:
        return cached
    content: list[dict[str, Any]] = [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]
    image_status = "not_available"
    if path is not None:
        try:
            image = ImageAdapter().prepare(str(path))
            content.append({"type": "image_url", "image_url": {"url": image.data_url}})
            image_status = "included"
        except ImageAdapterError as error:
            image_status = error.code
    response = client.complete(
        runtime.resource_model_key,
        [{"role": "system", "content": prompt["system"]}, {"role": "user", "content": content}],
        response_format={"type": "json_object"}, prompt_id=prompt["id"],
        prompt_version=str(prompt["version"]), input_unit_ids=[unit.id],
        max_tokens=int(prompt.get("max_tokens", 900)),
    )
    value = json.loads(response["choices"][0]["message"]["content"])
    content_kinds = value.get("content_kinds")
    if isinstance(content_kinds, str):
        value["content_kinds"] = [content_kinds]
    elif isinstance(content_kinds, dict):
        value["content_kinds"] = [str(key) for key, enabled in content_kinds.items() if enabled]
    elif not isinstance(content_kinds, list):
        value["content_kinds"] = [] if content_kinds is None else [str(content_kinds)]
    _validate_asset_result(value)
    result = {
        **value, "status": "ok", "image_status": image_status,
        "prompt_id": prompt["id"], "prompt_version": str(prompt["version"]),
        "configured_model": runtime.model_config[runtime.resource_model_key]["model"],
        "served_model": response.get("model"), "fingerprint": fingerprint,
    }
    _write_cache(cache_dir / f"{unit.id}.json", result)
    return result


def _understand_formula(graph, unit_id, positions, runtime, prompt, client, cache_dir) -> dict[str, Any]:
    unit = graph.units[unit_id]
    evidence_ids = _formula_context_ids(graph, unit_id, positions)
    payload = {
        "formula_unit_id": unit.id, "formula": unit.content,
        "context": [
            {"unit_id": evidence_id, "content": graph.units[evidence_id].content}
            for evidence_id in evidence_ids
        ],
    }
    fingerprint = _fingerprint(prompt, runtime, payload, None)
    cached = _read_cache(cache_dir / f"{unit.id}.json", fingerprint)
    if cached is not None:
        return cached
    response = client.complete(
        runtime.resource_model_key,
        [{"role": "system", "content": prompt["system"]}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        response_format={"type": "json_object"}, prompt_id=prompt["id"],
        prompt_version=str(prompt["version"]), input_unit_ids=[unit.id, *evidence_ids],
        max_tokens=int(prompt.get("max_tokens", 900)),
    )
    value = json.loads(response["choices"][0]["message"]["content"])
    _validate_formula_result(value, {unit.id, *evidence_ids})
    result = {
        **value, "status": "ok", "useful": bool(value.get("summary") or value.get("symbols")),
        "prompt_id": prompt["id"], "prompt_version": str(prompt["version"]),
        "configured_model": runtime.model_config[runtime.resource_model_key]["model"],
        "served_model": response.get("model"), "fingerprint": fingerprint,
    }
    _write_cache(cache_dir / f"{unit.id}.json", result)
    return result


def _formula_context_ids(graph: DocumentGraph, formula_id: str, positions: dict[str, int]) -> list[str]:
    linked = {
        relation.source_id for relation in graph.relations
        if relation.target_id == formula_id and relation.type in {"MENTIONS", "INTRODUCES", "ALIGNS_WITH"}
    }
    linked.update(
        relation.target_id for relation in graph.relations
        if relation.source_id == formula_id and relation.type == "EXPLAINED_BY"
    )
    order = ordered_units(graph)
    position = positions.get(formula_id)
    if position is not None:
        for candidate_id in order[max(0, position - 1):position + 2]:
            if graph.units[candidate_id].type in {"passage", "title"}:
                linked.add(candidate_id)
    return sorted(linked, key=lambda item: positions.get(item, 10**9))


def _resolve_asset_path(graph: DocumentGraph, unit) -> Path | None:
    raw = unit.source.asset_path
    if not raw:
        return None
    path = Path(raw)
    if path.is_absolute():
        return path if path.is_file() else None
    input_path = Path(str(graph.metadata.get("input_path") or ""))
    candidate = input_path.parent / path
    return candidate if candidate.is_file() else None


def _validate_asset_result(value: dict[str, Any]) -> None:
    if not isinstance(value.get("useful"), bool):
        raise ValueError("resource useful must be boolean")
    if not isinstance(value.get("description"), str):
        raise ValueError("resource description must be text")
    if not isinstance(value.get("content_kinds"), list):
        raise ValueError("resource content_kinds must be a list")


def _validate_formula_result(value: dict[str, Any], allowed_ids: set[str]) -> None:
    if not isinstance(value.get("symbols"), list):
        raise ValueError("formula symbols must be a list")
    for symbol in value["symbols"]:
        if not symbol.get("symbol") or not set(symbol.get("evidence_unit_ids", [])) <= allowed_ids:
            raise ValueError("formula symbol evidence is invalid")


def _fingerprint(prompt, runtime, payload, path: Path | None) -> str:
    file_hash = None
    if path is not None and path.is_file():
        file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    raw = json.dumps({
        "prompt": [prompt["id"], str(prompt["version"])],
        "model": runtime.model_config[runtime.resource_model_key]["model"],
        "payload": payload, "file_hash": file_hash,
    }, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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
