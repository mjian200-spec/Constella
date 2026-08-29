from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable

import yaml

from constella.context_builder.llm_client import LLMClient
from .io import write_jsonl


RELATION_TYPES = {"IS_A", "HAS_SUBTYPE", "PART_OF", "INCLUDES", "SAME_AS"}


@dataclass(slots=True)
class ArticleDiscoveryRuntime:
    model_key: str
    models: dict[str, Any]
    prompt_dir: Path
    max_workers: int = 16
    use_llm: bool = False


class PackageConceptProcessor:
    """Extract concepts only from Context Builder's already-routed semantic packages."""

    def __init__(self, runtime: ArticleDiscoveryRuntime, client=None) -> None:
        self.runtime = runtime
        self.client = client or LLMClient(runtime.models)
        self.prompts = {
            name: self._load(runtime.prompt_dir / filename) for name, filename in {
                "concepts": "package_concept_extractor_v1.yaml",
                "relations": "package_structure_extractor_v1.yaml",
                "audit": "package_concept_auditor_v1.yaml",
            }.items()
        }

    @staticmethod
    def _load(path: Path) -> dict[str, Any]:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not {"id", "version", "system"} <= set(value or {}):
            raise ValueError(f"Invalid prompt: {path}")
        return value

    def process(self, package: dict[str, Any], graph: dict[str, Any]) -> dict[str, Any]:
        routed = package.get("attributes", {}).get("package_role", {})
        if routed.get("status") != "ok":
            package.setdefault("attributes", {})["roles"] = {
                "is_rule_package": False, "is_concept_package": False,
                "is_useless": False, "status": "missing_context_builder_route",
            }
            return package
        roles = {
            "is_rule_package": bool(routed.get("is_rule_package")),
            "is_concept_package": bool(routed.get("is_concept_package")),
            "is_useless": bool(routed.get("is_useless")),
            "status": "reused_context_builder_route",
            "prompt_id": routed.get("prompt_id"),
            "prompt_version": routed.get("prompt_version"),
        }
        package.setdefault("attributes", {})["roles"] = roles
        if not roles["is_concept_package"] or not self.runtime.use_llm:
            return package
        payload = _package_payload(package, graph)
        concepts = self._call("concepts", payload, lambda value: self._validate_concepts(value, payload))
        relation_payload = {**payload, "extracted_concepts": concepts["concepts"]}
        relations = self._call("relations", relation_payload, lambda value: self._validate_relations(value, payload))
        audit_payload = {**payload, "concept_extraction": concepts, "relation_extraction": relations}
        audit = self._call("audit", audit_payload, lambda value: self._validate_audit(value, payload))
        package["concept_extraction"] = {"concepts": concepts["concepts"], "relations": relations["relations"], "audit": audit}
        return package

    def _call(self, key: str, payload: dict[str, Any], validator: Callable[[dict], None]) -> dict[str, Any]:
        prompt = self.prompts[key]
        messages = [{"role": "system", "content": prompt["system"]}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
        errors: list[str] = []
        for attempt in range(3):
            try:
                response = self.client.complete(
                    self.runtime.model_key, messages, response_format={"type": "json_object"},
                    prompt_id=prompt["id"], prompt_version=str(prompt["version"]),
                    input_unit_ids=payload["evidence_unit_ids"], max_tokens=int(prompt.get("max_tokens", 1600)),
                )
                value = json.loads(response["choices"][0]["message"]["content"])
                validator(value)
                value["prompt_id"] = prompt["id"]
                value["prompt_version"] = str(prompt["version"])
                return value
            except Exception as error:
                errors.append(str(error))
                if attempt < 2:
                    messages.append({"role": "user", "content": f"输出不合规：{error}。请只返回修正后的JSON。"})
        raise ValueError(f"{prompt['id']} failed validation: {' | '.join(errors)}")

    @staticmethod
    def _validate_concepts(value: dict, payload: dict) -> None:
        allowed = set(payload["evidence_unit_ids"])
        if not isinstance(value.get("concepts"), list):
            raise ValueError("concepts must be a list")
        for concept in value["concepts"]:
            if not concept.get("name") or not set(concept.get("name_evidence_unit_ids", [])) <= allowed:
                raise ValueError("invalid concept name evidence")
            definition = concept.get("explicit_definition")
            definition_ids = concept.get("definition_evidence_unit_ids", [])
            if bool(definition) != bool(definition_ids) or not set(definition_ids) <= allowed:
                raise ValueError("explicit definition and its evidence must occur together")

    @staticmethod
    def _validate_relations(value: dict, payload: dict) -> None:
        allowed = set(payload["evidence_unit_ids"])
        if not isinstance(value.get("relations"), list):
            raise ValueError("relations must be a list")
        for relation in value["relations"]:
            if relation.get("relation_type") not in RELATION_TYPES:
                raise ValueError("unsupported relation type")
            if not relation.get("source") or not relation.get("target"):
                raise ValueError("relation endpoints are required")
            if not relation.get("evidence_unit_ids") or not set(relation["evidence_unit_ids"]) <= allowed:
                raise ValueError("relation requires package evidence")

    @staticmethod
    def _validate_audit(value: dict, payload: dict) -> None:
        if value.get("status") not in {"accepted", "revised", "rejected"}:
            raise ValueError("invalid audit status")
        if not isinstance(value.get("concepts"), list) or not isinstance(value.get("relations"), list):
            raise ValueError("audit must return final concepts and relations")
        PackageConceptProcessor._validate_concepts({"concepts": value["concepts"]}, payload)
        PackageConceptProcessor._validate_relations({"relations": value["relations"]}, payload)
        names = {_normalize(item["name"]) for item in value["concepts"]}
        for relation in value["relations"]:
            if _normalize(relation["source"]) not in names or _normalize(relation["target"]) not in names:
                raise ValueError("audited relation endpoints must exist in audited concepts")


def run_article_concept_discovery(
    context_output_dir: str | Path, output_dir: str | Path, runtime: ArticleDiscoveryRuntime, *, limit: int | None = None, client=None,
) -> dict[str, int]:
    source = Path(context_output_dir)
    target = Path(output_dir)
    graph = json.loads((source / "document_graph.json").read_text(encoding="utf-8"))
    packages = [json.loads(line) for line in (source / "context_packages.jsonl").read_text(encoding="utf-8").splitlines() if line]
    if limit is not None:
        packages = packages[:limit]
    processor = PackageConceptProcessor(runtime, client)
    processed: list[dict[str, Any] | None] = [None] * len(packages)
    target.mkdir(parents=True, exist_ok=True)
    package_results_dir = target / "packages"
    package_results_dir.mkdir(exist_ok=True)
    existing = {}
    for path in package_results_dir.glob("*.json"):
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if result.get("attributes", {}).get("concept_discovery", {}).get("status") != "failed":
            existing[path.stem] = result
    pending: list[int] = []
    for index, package in enumerate(packages):
        if package["id"] in existing:
            processed[index] = existing[package["id"]]
        else:
            pending.append(index)
    with ThreadPoolExecutor(max_workers=runtime.max_workers) as pool:
        futures = {pool.submit(processor.process, packages[index], graph): index for index in pending}
        for future in as_completed(futures):
            index = futures[future]
            try:
                processed[index] = future.result()
            except Exception as error:
                failed = packages[index]
                failed.setdefault("attributes", {})["concept_discovery"] = {
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "reason": str(error),
                }
                processed[index] = failed
            _write_package_result(package_results_dir, processed[index])
    rows = [row for row in processed if row is not None]
    concepts, relations = _assemble(rows)
    write_jsonl(target / "context_packages.jsonl", rows)
    write_jsonl(target / "concepts.jsonl", concepts)
    write_jsonl(target / "concept_relations.jsonl", relations)
    return {
        "package_count": len(rows),
        "failed_package_count": sum(
            p.get("attributes", {}).get("concept_discovery", {}).get("status") == "failed"
            for p in rows
        ),
        "concept_package_count": sum(bool(p.get("attributes", {}).get("roles", {}).get("is_concept_package")) for p in rows),
        "concept_count": len(concepts),
        "relation_count": len(relations),
    }


def _write_package_result(packages_dir: Path, package: dict[str, Any]) -> None:
    path = packages_dir / f"{package['id']}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _package_payload(package: dict[str, Any], graph: dict[str, Any]) -> dict[str, Any]:
    """Pass Context Builder fields through without rebuilding or recalling context."""
    units = graph.get("units") or {}
    attributes = package.get("attributes") or {}
    core_ids = list(dict.fromkeys(package.get("core_unit_ids") or []))
    support_ids = [item for item in package.get("support_unit_ids") or [] if item not in core_ids]
    asset_ids = list(dict.fromkeys(package.get("asset_part_ids") or []))
    covered = set(core_ids) | set(support_ids)
    resource_ids = [item for item in asset_ids if item not in covered]
    constraints = graph.get("constraints") or {}
    constraint_rows = []
    constraint_source_ids: list[str] = []
    for constraint_id in package.get("constraint_ids") or []:
        constraint = constraints.get(constraint_id)
        if not constraint:
            continue
        source_id = str(constraint.get("source_id") or "")
        if source_id in units:
            constraint_source_ids.append(source_id)
        constraint_rows.append({
            "constraint_id": constraint_id,
            "type": constraint.get("type"),
            "value": constraint.get("value"),
            "scope": constraint.get("scope") or {},
            "status": constraint.get("status"),
            "source_unit": _unit_payload(source_id, units) if source_id in units else None,
        })
    evidence_unit_ids = list(dict.fromkeys([
        *core_ids, *support_ids, *asset_ids, *constraint_source_ids,
    ]))
    ambiguities = graph.get("ambiguities") or {}
    return {
        "package_id": package["id"],
        "package_role": attributes.get("package_role"),
        "section_path": attributes.get("section_path") or [],
        "candidate_sources": attributes.get("candidate_sources") or [],
        "heading_list_structure": attributes.get("heading_list_structure") or {},
        "core_units": [_unit_payload(unit_id, units) for unit_id in core_ids if unit_id in units],
        "support_units": [_unit_payload(unit_id, units) for unit_id in support_ids if unit_id in units],
        "resources": [_unit_payload(unit_id, units) for unit_id in resource_ids if unit_id in units],
        "constraints": constraint_rows,
        "unresolved": [ambiguities[item] for item in package.get("unresolved_ids") or [] if item in ambiguities],
        "evidence_unit_ids": [item for item in evidence_unit_ids if item in units],
    }


def _unit_payload(unit_id: str, units: dict[str, Any]) -> dict[str, Any]:
    unit = units[unit_id]
    attributes = unit.get("attributes") or {}
    return {
        "unit_id": unit_id,
        "type": unit.get("type"),
        "content": unit.get("content"),
        "caption": attributes.get("caption"),
        "table_body": attributes.get("table_body"),
        "resource_understanding": attributes.get("resource_understanding"),
    }


def _assemble(packages: list[dict[str, Any]]) -> tuple[list[dict], list[dict]]:
    accepted_concepts: dict[str, dict] = {}
    raw_relations: list[tuple[str, dict, str]] = []
    aliases: list[tuple[str, str]] = []
    for package in packages:
        extraction = package.get("concept_extraction")
        if not extraction:
            continue
        audit = extraction.get("audit", {})
        if audit.get("status") == "rejected":
            continue
        audit_status = audit.get("status", "accepted")
        for item in audit.get("concepts", []):
            name = _normalize(item["name"])
            record = accepted_concepts.setdefault(name, {"canonical_name": item["name"], "aliases": [], "definition": None, "definition_type": None, "source_package_ids": [], "evidence_ids": [], "audit_status": audit_status})
            if audit_status == "revised":
                record["audit_status"] = "revised"
            record["source_package_ids"].append(package["id"])
            record["evidence_ids"].extend(item.get("name_evidence_unit_ids", []))
            if item.get("explicit_definition") and record["definition"] is None:
                record["definition"] = item["explicit_definition"]; record["definition_type"] = "explicit"
                record["evidence_ids"].extend(item.get("definition_evidence_unit_ids", []))
        for relation in audit.get("relations", []):
            if relation["relation_type"] == "SAME_AS":
                aliases.append((_normalize(relation["source"]), _normalize(relation["target"])))
            else:
                raw_relations.append((package["id"], relation, audit_status))
    alias_redirects = _merge_alias_components(accepted_concepts, aliases)
    concepts: list[dict] = []
    name_to_id: dict[str, str] = {}
    for name, record in accepted_concepts.items():
        concept_id = "concept_" + hashlib.sha1(name.encode()).hexdigest()[:12]
        name_to_id[name] = concept_id
        record.update({"concept_id": concept_id, "source_seed_ids": [], "origin_depth": 0})
        for key in ("aliases", "source_package_ids", "evidence_ids"):
            record[key] = list(dict.fromkeys(record[key]))
        concepts.append(record)
    for alias, canonical in alias_redirects.items():
        while canonical in alias_redirects:
            canonical = alias_redirects[canonical]
        if canonical in name_to_id:
            name_to_id[alias] = name_to_id[canonical]
    formal: list[dict] = []
    for package_id, relation, audit_status in raw_relations:
        source, target, kind = relation["source"], relation["target"], relation["relation_type"]
        if kind == "HAS_SUBTYPE": source, target, kind = target, source, "IS_A"
        elif kind == "INCLUDES": source, target, kind = target, source, "PART_OF"
        source_id, target_id = name_to_id.get(_normalize(source)), name_to_id.get(_normalize(target))
        if not source_id or not target_id or source_id == target_id:
            continue
        formal.append({"relation_id": "relation_" + hashlib.sha1(f"{kind}:{source_id}:{target_id}".encode()).hexdigest()[:12], "child_concept_id": source_id, "type": kind, "parent_concept_id": target_id, "directness": "direct", "evidence_ids": relation["evidence_unit_ids"], "source_package_ids": [package_id], "original_relation": relation, "audit_status": audit_status})
    unique: dict[tuple[str, str, str], dict] = {}
    for row in formal:
        key = (row["type"], row["child_concept_id"], row["parent_concept_id"])
        existing = unique.get(key)
        if existing is None:
            row["original_relations"] = [row["original_relation"]]
            unique[key] = row
            continue
        existing["evidence_ids"] = list(dict.fromkeys(existing["evidence_ids"] + row["evidence_ids"]))
        existing["source_package_ids"] = list(dict.fromkeys(existing["source_package_ids"] + row["source_package_ids"]))
        existing["original_relations"].append(row["original_relation"])
        if row["audit_status"] == "revised":
            existing["audit_status"] = "revised"
    return concepts, _direct_relations(list(unique.values()))


def _direct_relations(relations: list[dict]) -> list[dict]:
    """Keep an acyclic, transitively reduced IS_A hierarchy from explicit package evidence."""
    is_a = sorted((row for row in relations if row["type"] == "IS_A"), key=lambda row: row["relation_id"])
    other = [row for row in relations if row["type"] != "IS_A"]
    kept: list[dict] = []
    for row in is_a:
        if not _reachable(row["parent_concept_id"], row["child_concept_id"], kept):
            kept.append(row)
    direct = [
        row for row in kept
        if not _reachable(row["child_concept_id"], row["parent_concept_id"], [item for item in kept if item is not row])
    ]
    return [*other, *direct]


def _reachable(start: str, target: str, relations: list[dict]) -> bool:
    parents: dict[str, set[str]] = {}
    for row in relations:
        parents.setdefault(row["child_concept_id"], set()).add(row["parent_concept_id"])
    pending, seen = [start], set()
    while pending:
        current = pending.pop()
        if current == target:
            return True
        if current not in seen:
            seen.add(current)
            pending.extend(parents.get(current, set()) - seen)
    return False


def _merge_alias_components(
    concepts: dict[str, dict], aliases: list[tuple[str, str]],
) -> dict[str, str]:
    """Merge SAME_AS components independent of relation input order.

    A target which is not itself redirected inside its component is preferred as
    the canonical spelling (A->B, B->C therefore resolves to C). Cycles and
    multiple sinks fall back to normalized lexical order for determinism.
    """
    parent = {name: name for name in concepts}

    def find(name: str) -> str:
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    valid_aliases = [
        (left, right) for left, right in aliases
        if left in concepts and right in concepts and left != right
    ]
    for left, right in valid_aliases:
        union(left, right)

    components: dict[str, list[str]] = {}
    for name in concepts:
        components.setdefault(find(name), []).append(name)
    outgoing = {left for left, _ in valid_aliases}
    redirects: dict[str, str] = {}
    merged: dict[str, dict] = {}
    for members in components.values():
        sinks = sorted(name for name in members if name not in outgoing)
        canonical = sinks[0] if sinks else sorted(members)[0]
        records = [concepts[name] for name in sorted(members)]
        canonical_record = concepts[canonical]
        record = {
            **canonical_record,
            "aliases": [],
            "source_package_ids": [],
            "evidence_ids": [],
        }
        definition_source = next(
            (item for item in [canonical_record, *records] if item.get("definition")),
            None,
        )
        if definition_source:
            record["definition"] = definition_source["definition"]
            record["definition_type"] = definition_source["definition_type"]
        for name, item in zip(sorted(members), records, strict=True):
            if name != canonical:
                record["aliases"].append(item["canonical_name"])
                redirects[name] = canonical
            record["aliases"].extend(item.get("aliases", []))
            record["source_package_ids"].extend(item["source_package_ids"])
            record["evidence_ids"].extend(item["evidence_ids"])
        record["aliases"] = list(dict.fromkeys(
            alias for alias in record["aliases"]
            if _normalize(alias) != canonical
        ))
        merged[canonical] = record
    concepts.clear()
    concepts.update(merged)
    return redirects


def _normalize(value: str) -> str:
    return "".join(str(value).split()).lower()
