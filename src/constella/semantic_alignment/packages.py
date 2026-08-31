from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .lifecycle import LONG_TAIL_MAX_OCCURRENCE, RankConfidence, rank_by_occurrence
from .models import AlignmentStatus, ConceptType, PackageTier, TIER_ORDER
from .registry import CharNgramIndex, ConceptRegistry, MemorySnapshot, normalize_text, stable_id


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"Expected JSON object at {path}:{line_number}")
            rows.append(value)
    return rows


def _file_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(slots=True)
class AlignmentInputs:
    concepts: list[dict[str, Any]]
    relations: list[dict[str, Any]]
    rules: list[dict[str, Any]]
    context_packages: dict[str, dict[str, Any]]
    units: dict[str, dict[str, Any]]
    input_manifest: dict[str, Any] = field(default_factory=dict)


def load_alignment_inputs(
    rule_output_dir: str | Path,
    concept_output_dir: str | Path,
    context_output_dir: str | Path | None = None,
) -> AlignmentInputs:
    rule_dir = Path(rule_output_dir)
    concept_dir = Path(concept_output_dir)
    rule_path = rule_dir / "structured_rules.jsonl"
    concept_path = concept_dir / "concepts.jsonl"
    relation_path = concept_dir / "concept_relations.jsonl"
    context_packages: dict[str, dict[str, Any]] = {}
    processed = rule_dir / "processed_context_packages.jsonl"
    if processed.is_file():
        context_packages = {str(row["id"]): row for row in _read_jsonl(processed)}
    units: dict[str, dict[str, Any]] = {}
    graph_path: Path | None = None
    if context_output_dir is not None:
        graph_path = Path(context_output_dir) / "document_graph.json"
        if graph_path.is_file():
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
            units = graph.get("units") or {}
    files = [rule_path, concept_path, relation_path]
    if processed.is_file():
        files.append(processed)
    if graph_path and graph_path.is_file():
        files.append(graph_path)
    return AlignmentInputs(
        concepts=_read_jsonl(concept_path),
        relations=_read_jsonl(relation_path),
        rules=_read_jsonl(rule_path),
        context_packages=context_packages,
        units=units,
        input_manifest={
            "files": [{"path": str(path), "sha256": _file_fingerprint(path)} for path in files],
        },
    )


_STRUCTURE_PATTERNS = (
    re.compile(r"[、,，/；;]|(?:以及|及其|和|与|或)"),
    re.compile(r"(?:当|在)?.*(?:超过|大于|小于|高于|低于|不少于|不超过|≥|≤|>|<).*[0-9]"),
    re.compile(
        r"[0-9]+(?:\.[0-9]+)?\s*(?:°C|℃|K|m?A|kA|m?V|kV|mm|cm|μm|%)(?:时|的)?",
        re.IGNORECASE,
    ),
    re.compile(r"(?:时|情况下|条件下|处于|正在|中的|状态的)"),
)


class SemanticPackageBuilder:
    """Collect sources, score object interpretations, and build tier-homogeneous packages."""

    def __init__(self, inputs: AlignmentInputs, *, memory: MemorySnapshot | None = None) -> None:
        self.inputs = inputs
        self.memory = memory or MemorySnapshot.build(inputs.concepts, inputs.relations)
        self.registry = ConceptRegistry(self.memory)
        self._candidate_cache: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.source_occurrence_count = 0
        self.state_rows, self.object_rows = self._collect_sources()
        self.mechanical_interpretations: dict[str, dict[str, Any]] = {}
        self.scored_cases = self._score_objects()

    def object_alignment_packages(
        self,
        *,
        candidates_per_object: int = 6,
        objects_per_package: int = 12,
        max_package_chars: int = 40_000,
    ) -> list[dict[str, Any]]:
        if objects_per_package < 1:
            raise ValueError("objects_per_package must be at least 1")
        if max_package_chars < 2_000:
            raise ValueError("max_package_chars must be at least 2000")
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for source in self.scored_cases:
            if source["object_id"] in self.mechanical_interpretations:
                continue
            case = {
                "object_id": source["object_id"],
                "tier": str(source["tier"]),
                "name": source["name"],
                "raw_variants": source["raw_variants"],
                "frequency": source["frequency"],
                "object_state_examples": [{
                    "object": source["name"],
                    "state": example["raw_state"],
                    "expression": f"{source['name']} | {example['raw_state']}",
                    "frequency": example["frequency"],
                } for example in source["state_examples"]],
                "contexts": source["contexts"],
                "confidence": source["confidence"],
                "rank_confidence": source["rank_confidence"],
                "occurrence_rank": source["occurrence_rank"],
                "rank_population": source.get("rank_population"),
                "long_tail_fallback_required": (
                    source["rank_confidence"] == RankConfidence.LOW
                    and int(source["frequency"]) <= LONG_TAIL_MAX_OCCURRENCE
                    and source["exact_resolution"].get("status") != AlignmentStatus.MATCHED
                ),
                "lexical_coverage": source["lexical_coverage"],
                "structure_signal_count": source["structure_signal_count"],
                "exact_resolution": self._formal_exact_resolution(source["exact_resolution"]),
                "candidates": self._object_candidates(source["name"], candidates_per_object),
            }
            grouped[str(source["tier"])].append(case)
        packages: list[dict[str, Any]] = []
        for tier in sorted(grouped, key=lambda value: TIER_ORDER[PackageTier(value)]):
            cases = sorted(grouped[tier], key=lambda row: (-int(row["frequency"]), row["object_id"]))
            current: list[dict[str, Any]] = []
            # Serialized size grows linearly with each appended case; track it
            # incrementally instead of re-dumping the whole package per case.
            current_size = self._package_payload_base_size(tier)
            for case in cases:
                case_size = len(json.dumps(case, ensure_ascii=False, separators=(",", ":"))) + 1
                if current and (len(current) >= objects_per_package or current_size + case_size > max_package_chars):
                    packages.append(self._finalize_package(tier, current))
                    current = [case]
                    current_size = self._package_payload_base_size(tier) + case_size
                else:
                    current.append(case)
                    current_size += case_size
            if current:
                packages.append(self._finalize_package(tier, current))
        return packages

    def _object_candidates(self, name: str, top_k: int) -> list[dict[str, Any]]:
        key = (name, top_k)
        if key not in self._candidate_cache:
            self._candidate_cache[key] = self.registry.candidates(
                name, concept_type=ConceptType.OBJECT, top_k=top_k,
            )
        return self._candidate_cache[key]

    @staticmethod
    def _formal_exact_resolution(resolution: dict[str, Any]) -> dict[str, Any]:
        if resolution.get("status") in {AlignmentStatus.MATCHED, AlignmentStatus.TYPE_REVIEW}:
            return {
                **resolution,
                "candidates": [
                    row for row in resolution.get("candidates") or []
                    if row.get("registration_status") == "APPROVED"
                ],
            }
        return {
            "status": AlignmentStatus.EXPRESSION_ONLY,
            "concept_id": None,
            "match_method": "NONE",
            "candidates": [],
        }

    def selected_object_ids(
        self,
        packages: list[dict[str, Any]],
        *,
        include_mechanical: bool,
        max_tier: str = PackageTier.H3,
    ) -> set[str]:
        result = {
            str(case["object_id"])
            for package in packages
            for case in package["cases"]
        }
        if include_mechanical:
            result.update(
                object_id for object_id, interpretation in self.mechanical_interpretations.items()
                if TIER_ORDER[PackageTier(interpretation["tier"])] <= TIER_ORDER[PackageTier(max_tier)]
            )
        return result

    def package_report(self, packages: list[dict[str, Any]]) -> dict[str, Any]:
        sizes = [len(json.dumps(package, ensure_ascii=False)) for package in packages]
        return {
            "mechanical_object_count": len(self.mechanical_interpretations),
            "mechanical_object_count_by_tier": dict(Counter(
                str(row["tier"]) for row in self.mechanical_interpretations.values()
            )),
            "llm_package_count": len(packages),
            "llm_case_count": sum(len(package["cases"]) for package in packages),
            "package_count_by_tier": dict(Counter(str(package["tier"]) for package in packages)),
            "case_count_by_tier": dict(Counter(
                str(package["tier"]) for package in packages for _case in package["cases"]
            )),
            "max_package_chars": max(sizes, default=0),
            "average_package_chars": round(sum(sizes) / len(sizes), 2) if sizes else 0.0,
        }

    def _score_objects(self) -> list[dict[str, Any]]:
        mechanical: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        for object_id, item in self.object_rows.items():
            exact = self.registry.resolve_exact(item["name"], concept_type=ConceptType.OBJECT)
            signals = sum(bool(pattern.search(item["name"])) for pattern in _STRUCTURE_PATTERNS)
            coverage = self.registry.lexical_coverage(item["name"], concept_type=ConceptType.OBJECT)
            if exact["status"] == AlignmentStatus.MATCHED and signals == 0:
                self.mechanical_interpretations[object_id] = {
                    "object_id": object_id,
                    "decision": "ATOMIC",
                    "core_objects": [{
                        "text": item["name"],
                        "concept_id": exact["concept_id"],
                        "match_method": exact["match_method"],
                    }],
                    "embedded_states": [],
                    "qualifiers": [],
                    "interpretation_method": "typed_exact",
                    "tier": PackageTier.H0,
                }
                mechanical.append({
                    **item,
                    "tier": PackageTier.H0,
                    "confidence": 1.0,
                    "rank_confidence": RankConfidence.HIGH,
                    "occurrence_rank": 0,
                    "lexical_coverage": coverage,
                    "structure_signal_count": signals,
                    "exact_resolution": exact,
                })
            else:
                unresolved.append({
                    **item,
                    "candidate_id": object_id,
                    "occurrence_count": int(item["frequency"]),
                    "lexical_coverage": coverage,
                    "structure_signal_count": signals,
                    "exact_resolution": exact,
                })

        ranked = rank_by_occurrence(
            unresolved, count_field="occurrence_count", identity_field="candidate_id",
        )
        tier_by_confidence = {
            RankConfidence.HIGH: PackageTier.H1,
            RankConfidence.MEDIUM: PackageTier.H2,
            RankConfidence.LOW: PackageTier.H3,
        }
        for row in ranked:
            row["tier"] = tier_by_confidence[row["rank_confidence"]]
            row["confidence"] = round(
                1 - (int(row["occurrence_rank"]) - 1) / len(ranked), 4,
            )
        return sorted([*mechanical, *ranked], key=lambda row: (
            TIER_ORDER[PackageTier(row["tier"])], -float(row["confidence"]),
            -int(row["frequency"]), row["object_id"],
        ))

    def _collect_sources(self) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        states: dict[str, dict[str, Any]] = {}
        objects: dict[str, dict[str, Any]] = {}
        object_names: dict[str, Counter[str]] = defaultdict(Counter)
        object_states: dict[str, Counter[str]] = defaultdict(Counter)
        object_contexts: dict[str, Counter[tuple[str, str]]] = defaultdict(Counter)
        for rule in self.inputs.rules:
            for role in ("conditions", "antecedents", "consequents"):
                for state in rule.get(role) or []:
                    self.source_occurrence_count += 1
                    state_id = str(state.get("id") or "")
                    raw_object = str(state.get("object") or "")
                    raw_state = str(state.get("raw_state") or "")
                    normalized_state = str(state.get("normalized_state") or "")
                    if not state_id or not raw_object:
                        raise ValueError(f"rule state requires id and object: {rule.get('id')}")
                    object_id = stable_id("object", normalize_text(raw_object))
                    existing = states.get(state_id)
                    identity = (raw_object, raw_state, normalized_state)
                    if existing and existing["identity"] != identity:
                        raise ValueError(f"state_id has inconsistent identity: {state_id}")
                    row = states.setdefault(state_id, {
                        "source_state_id": state_id,
                        "identity": identity,
                        "object_id": object_id,
                        "raw_object": raw_object,
                        "raw_state": raw_state,
                        "normalized_state": normalized_state,
                        "frequency": 0,
                        "rule_ids": set(),
                        "context_package_ids": set(),
                        "roles": set(),
                        "contexts": [],
                    })
                    row["frequency"] += 1
                    row["rule_ids"].add(str(rule.get("id") or ""))
                    if rule.get("context_package_id"):
                        row["context_package_ids"].add(str(rule["context_package_id"]))
                    row["roles"].add(role[:-1] if role.endswith("s") else role)
                    context = self._state_context(rule, role, state_id)
                    if context not in row["contexts"] and len(row["contexts"]) < 3:
                        row["contexts"].append(context)
                    object_row = objects.setdefault(object_id, {
                        "object_id": object_id,
                        "object_key": normalize_text(raw_object),
                        "frequency": 0,
                        "source_state_ids": set(),
                    })
                    object_row["frequency"] += 1
                    object_row["source_state_ids"].add(state_id)
                    object_names[object_id][raw_object] += 1
                    object_states[object_id][raw_state] += 1
                    for counterpart in context["counterparts"][:2]:
                        object_contexts[object_id][(
                            str(rule.get("relation") or ""), counterpart,
                        )] += 1
        final_states: dict[str, dict[str, Any]] = {}
        for state_id, row in states.items():
            final_states[state_id] = {
                **{key: value for key, value in row.items() if key != "identity"},
                "rule_ids": sorted(value for value in row["rule_ids"] if value),
                "context_package_ids": sorted(row["context_package_ids"]),
                "roles": sorted(row["roles"]),
            }
        final_objects: dict[str, dict[str, Any]] = {}
        for object_id, row in objects.items():
            names = [value for value, _count in object_names[object_id].most_common()]
            final_objects[object_id] = {
                **row,
                "source_state_ids": sorted(row["source_state_ids"]),
                "name": names[0],
                "raw_variants": names,
                "state_examples": [
                    {"raw_state": value, "frequency": count}
                    for value, count in object_states[object_id].most_common(5)
                    if value
                ],
                "contexts": [
                    {"relation": relation, "counterpart": counterpart}
                    for (relation, counterpart), _count in object_contexts[object_id].most_common(2)
                ],
            }
        return final_states, final_objects

    def _package_payload(self, tier: str, cases: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "package_type": "object_alignment",
            "tier": tier,
            "memory_version": self.memory.version,
            "cases": cases,
        }

    def _package_payload_base_size(self, tier: str) -> int:
        return len(json.dumps(self._package_payload(tier, []), ensure_ascii=False, separators=(",", ":")))

    def _finalize_package(self, tier: str, cases: list[dict[str, Any]]) -> dict[str, Any]:
        payload = self._package_payload(tier, cases)
        payload["package_id"] = stable_id("object_alignment", payload)
        return payload

    @staticmethod
    def _state_context(rule: dict[str, Any], role: str, state_id: str) -> dict[str, Any]:
        counterparts = []
        for side in ("conditions", "antecedents", "consequents"):
            for other in rule.get(side) or []:
                if str(other.get("id") or "") != state_id:
                    counterparts.append(f"{other.get('object', '')}|{other.get('raw_state', '')}")
        return {
            "role": role[:-1] if role.endswith("s") else role,
            "relation": rule.get("relation"),
            "counterparts": counterparts[:3],
            "raw_expression": rule.get("raw_expression"),
        }


__all__ = [
    "AlignmentInputs", "CharNgramIndex", "SemanticPackageBuilder", "load_alignment_inputs",
    "normalize_text", "stable_id",
]
