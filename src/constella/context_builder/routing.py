from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

import yaml

from .llm_client import LLMClient
from .models import DocumentGraph, PipelineRuntime
from .pattern_engine import PatternEngine


LLM_ROUTE_MARKERS = re.compile(
    r"因此|所以|从而|导致|使得|影响|关系|随着|条件|当.+时|在.+时|"
    r"增加|减少|提高|降低|产生|形成|稳定|不稳定"
)
ALLOWED_LLM_ROLES = {"rule", "structured_candidate", "support", "unknown"}


def finalize_routes(graph: DocumentGraph, runtime: PipelineRuntime, patterns: PatternEngine) -> None:
    for unit in graph.units.values():
        candidates = unit.attributes.setdefault("route_candidates", [])
        for match in patterns.match("rule_language", unit):
            candidates.append({"role": "rule", "pattern_id": match.pattern_id, "confidence": match.confidence})
        for match in patterns.match("structured_language", unit):
            candidates.append({"role": "structured_candidate", "pattern_id": match.pattern_id, "confidence": match.confidence})
        for candidate in candidates:
            if candidate["confidence"] >= 0.75 and candidate["role"] not in unit.role:
                unit.role.append(candidate["role"])
        if unit.type in {"title", "caption"} and unit.role == []:
            unit.role.append("support")
    if runtime.use_llm:
        _classify_low_confidence_routes(graph, runtime)
    runtime.record(stage="finalize_routes", rule_units=sum("rule" in unit.role for unit in graph.units.values()))


def _classify_low_confidence_routes(graph: DocumentGraph, runtime: PipelineRuntime) -> None:
    """Use the local model only to choose a route for soft, non-deterministic candidates."""
    prompt = _load_prompt(runtime)
    candidates = [
        unit for unit in graph.units.values()
        if _is_low_confidence_route_candidate(unit)
    ]
    batch_size = 12
    max_batches = runtime.llm_max_batches
    client = LLMClient(runtime.model_config, event_sink=runtime.record)
    processed = 0
    for batch_index, start in enumerate(range(0, len(candidates), batch_size)):
        if max_batches is not None and batch_index >= max_batches:
            break
        batch = candidates[start:start + batch_size]
        unit_ids = [unit.id for unit in batch]
        messages = [
            {"role": "system", "content": prompt["system"]},
            {"role": "user", "content": _route_request(batch)},
        ]
        try:
            response = client.complete(
                "small", messages, response_format={"type": "json_object"},
                prompt_id=prompt["id"], prompt_version=prompt["version"], input_unit_ids=unit_ids,
                max_tokens=1024,
            )
            labels = _validated_labels(response, set(unit_ids))
        except Exception as error:
            for unit in batch:
                unit.attributes["llm_route"] = {
                    "prompt_id": prompt["id"], "prompt_version": prompt["version"],
                    "status": f"error:{type(error).__name__}", "selected_role": "unknown",
                }
            continue
        for unit in batch:
            role = labels.get(unit.id, "unknown")
            unit.attributes["llm_route"] = {
                "prompt_id": prompt["id"], "prompt_version": prompt["version"],
                "status": "ok", "selected_role": role,
            }
            if role == "unknown":
                continue
            unit.attributes["route_candidates"].append({
                "role": role, "pattern_id": "llm.route_classifier_v2", "confidence": 0.6,
                "source": "llm", "prompt_id": prompt["id"], "prompt_version": prompt["version"],
            })
            if role not in unit.role:
                unit.role.append(role)
        processed += len(batch)
    runtime.record(stage="llm_route_classification", candidates=len(candidates), processed=processed, batches=(processed + batch_size - 1) // batch_size)


def _is_low_confidence_route_candidate(unit) -> bool:
    if unit.type != "passage" or not isinstance(unit.content, str) or "noise" in unit.role:
        return False
    if any(candidate.get("confidence", 0) >= 0.75 for candidate in unit.attributes.get("route_candidates", [])):
        return False
    return len(unit.content) >= 20 and bool(LLM_ROUTE_MARKERS.search(unit.content))


def _load_prompt(runtime: PipelineRuntime) -> dict[str, Any]:
    path = runtime.config_dir.parents[1] / "prompts" / "context_builder" / "route_classifier_v2.yaml"
    prompt = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not {"id", "version", "system", "output_schema"}.issubset(prompt):
        raise ValueError(f"Invalid route prompt: {path}")
    return prompt


def _route_request(units: list) -> str:
    records = [
        {"unit_id": unit.id, "type": unit.type, "section_path": unit.attributes.get("section_path", []), "content": unit.content}
        for unit in units
    ]
    return "Classify this JSON array. Return only a JSON object matching the required schema.\n" + json.dumps(records, ensure_ascii=False)


def _validated_labels(response: dict[str, Any], allowed_unit_ids: set[str]) -> dict[str, str]:
    try:
        content = response["choices"][0]["message"]["content"]
        parsed = json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("Model response is not a JSON label object") from error
    labels: dict[str, str] = {}
    for item in parsed.get("labels", []):
        if not isinstance(item, dict):
            continue
        unit_id, role = item.get("unit_id"), item.get("role")
        if unit_id in allowed_unit_ids and role in ALLOWED_LLM_ROLES and unit_id not in labels:
            labels[unit_id] = role
    return labels
