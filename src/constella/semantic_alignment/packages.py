from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable


def normalize_text(value: str) -> str:
    return "".join(str(value).split()).lower()


def stable_id(prefix: str, value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}_{hashlib.sha256(payload.encode()).hexdigest()[:16]}"


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


@dataclass(slots=True)
class AlignmentInputs:
    concepts: list[dict[str, Any]]
    relations: list[dict[str, Any]]
    rules: list[dict[str, Any]]
    context_packages: dict[str, dict[str, Any]]
    units: dict[str, dict[str, Any]]


def load_alignment_inputs(
    rule_output_dir: str | Path,
    concept_output_dir: str | Path,
    context_output_dir: str | Path | None = None,
) -> AlignmentInputs:
    rule_dir = Path(rule_output_dir)
    concept_dir = Path(concept_output_dir)
    context_packages: dict[str, dict[str, Any]] = {}
    processed = rule_dir / "processed_context_packages.jsonl"
    if processed.is_file():
        context_packages = {row["id"]: row for row in _read_jsonl(processed)}
    units: dict[str, dict[str, Any]] = {}
    if context_output_dir is not None:
        graph_path = Path(context_output_dir) / "document_graph.json"
        if graph_path.is_file():
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
            units = graph.get("units") or {}
    return AlignmentInputs(
        concepts=_read_jsonl(concept_dir / "concepts.jsonl"),
        relations=_read_jsonl(concept_dir / "concept_relations.jsonl"),
        rules=_read_jsonl(rule_dir / "structured_rules.jsonl"),
        context_packages=context_packages,
        units=units,
    )


class CharNgramIndex:
    """Dependency-free sparse character n-gram TF-IDF candidate index."""

    def __init__(self, documents: dict[str, str], *, ngrams: tuple[int, ...] = (2, 3)) -> None:
        self.ngrams = ngrams
        self.size = len(documents)
        terms = {key: Counter(self._terms(text)) for key, text in documents.items()}
        document_frequency: Counter[str] = Counter()
        for counts in terms.values():
            document_frequency.update(counts.keys())
        self.idf = {
            term: math.log((1 + self.size) / (1 + frequency)) + 1
            for term, frequency in document_frequency.items()
        }
        self.vectors = {key: self._vector(counts) for key, counts in terms.items()}
        self.inverted: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for key, vector in self.vectors.items():
            for term, weight in vector.items():
                self.inverted[term].append((key, weight))

    def _terms(self, text: str) -> Iterable[str]:
        normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(text).lower())
        if not normalized:
            return []
        result: list[str] = []
        for size in self.ngrams:
            if len(normalized) < size:
                continue
            result.extend(normalized[index:index + size] for index in range(len(normalized) - size + 1))
        return result or [normalized]

    def _vector(self, counts: Counter[str]) -> dict[str, float]:
        weighted = {term: (1 + math.log(count)) * self.idf.get(term, 1.0) for term, count in counts.items()}
        norm = math.sqrt(sum(value * value for value in weighted.values()))
        return {term: value / norm for term, value in weighted.items()} if norm else {}

    def query(self, text: str, *, top_k: int, exclude: set[str] | None = None) -> list[tuple[str, float]]:
        vector = self._vector(Counter(self._terms(text)))
        scores: Counter[str] = Counter()
        for term, query_weight in vector.items():
            for key, weight in self.inverted.get(term, []):
                scores[key] += query_weight * weight
        excluded = exclude or set()
        return [(key, score) for key, score in scores.most_common() if key not in excluded][:top_k]


class SemanticPackageBuilder:
    def __init__(self, inputs: AlignmentInputs) -> None:
        self.inputs = inputs
        self.concepts = {str(row["concept_id"]): row for row in inputs.concepts}
        self.object_rows = self._collect_objects()
        self.name_to_concepts: dict[str, list[str]] = defaultdict(list)
        for concept_id, concept in self.concepts.items():
            for name in [concept.get("canonical_name"), *(concept.get("aliases") or [])]:
                if name:
                    self.name_to_concepts[normalize_text(str(name))].append(concept_id)

    def concept_merge_packages(
        self, *, candidates_per_anchor: int = 5, anchors_per_package: int = 10,
    ) -> list[dict[str, Any]]:
        signatures = {concept_id: self._concept_signature(row) for concept_id, row in self.concepts.items()}
        index = CharNgramIndex(signatures)
        seen_pairs: set[tuple[str, str]] = set()
        cases: list[dict[str, Any]] = []
        for concept_id in sorted(self.concepts):
            candidates: list[dict[str, Any]] = []
            for candidate_id, _score in index.query(
                signatures[concept_id], top_k=candidates_per_anchor * 3, exclude={concept_id},
            ):
                pair = tuple(sorted((concept_id, candidate_id)))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                candidates.append(self._concept_payload(candidate_id))
                if len(candidates) >= candidates_per_anchor:
                    break
            if candidates:
                cases.append({"anchor": self._concept_payload(concept_id), "candidates": candidates})
        return self._chunk("concept_merge", cases, anchors_per_package, "cases")

    def object_alignment_packages(
        self,
        concepts: list[dict[str, Any]] | None = None,
        *,
        candidates_per_object: int = 8,
        objects_per_package: int = 15,
    ) -> list[dict[str, Any]]:
        available = {str(row["concept_id"]): row for row in (concepts or self.inputs.concepts)}
        signatures = {concept_id: self._concept_signature(row) for concept_id, row in available.items()}
        index = CharNgramIndex(signatures)
        cases: list[dict[str, Any]] = []
        ordered_object_keys = sorted(
            self.object_rows,
            key=lambda key: (-int(self.object_rows[key]["frequency"]), key),
        )
        for object_key in ordered_object_keys:
            item = self.object_rows[object_key]
            signature = "\n".join([
                item["name"], *item["states"],
                *(context["relation"] for context in item["contexts"] if context.get("relation")),
                *(context["counterpart"] for context in item["contexts"] if context.get("counterpart")),
            ])
            candidate_ids: list[str] = []
            for exact_id in self.name_to_concepts.get(object_key, []):
                if exact_id in available and exact_id not in candidate_ids:
                    candidate_ids.append(exact_id)
            for candidate_id, _score in index.query(signature, top_k=candidates_per_object * 2):
                if candidate_id not in candidate_ids:
                    candidate_ids.append(candidate_id)
                if len(candidate_ids) >= candidates_per_object:
                    break
            cases.append({
                "object_id": item["object_id"],
                "name": item["name"],
                "frequency": item["frequency"],
                "states": item["states"],
                "contexts": item["contexts"],
                "candidates": [self._concept_payload(value, available) for value in candidate_ids],
            })
        return self._chunk("object_alignment", cases, objects_per_package, "cases")

    def state_normalization_packages(
        self,
        alignments: dict[str, str],
        concepts: list[dict[str, Any]] | None = None,
        *,
        states_per_package: int = 20,
    ) -> list[dict[str, Any]]:
        available = {str(row["concept_id"]): row for row in (concepts or self.inputs.concepts)}
        object_to_concept = {
            object_key: alignments.get(item["object_id"])
            for object_key, item in self.object_rows.items()
        }
        grouped: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        for rule in self.inputs.rules:
            for role in ("conditions", "antecedents", "consequents"):
                for state in rule.get(role) or []:
                    concept_id = object_to_concept.get(normalize_text(str(state.get("object") or "")))
                    if not concept_id or concept_id in {"NEW", "REPARSE", "INVALID"} or concept_id not in available:
                        continue
                    state_id = str(state.get("id") or "")
                    record = grouped[concept_id].setdefault(state_id, {
                        "id": state_id,
                        "text": str(state.get("raw_state") or ""),
                        "current_normalized": str(state.get("normalized_state") or ""),
                        "contexts": [],
                    })
                    context = self._state_context(rule, role, state_id)
                    if context not in record["contexts"] and len(record["contexts"]) < 2:
                        record["contexts"].append(context)
        packages: list[dict[str, Any]] = []
        for concept_id in sorted(grouped):
            states = list(grouped[concept_id].values())
            for state_chunk in self._cluster_states(states, states_per_package):
                payload = {
                    "package_type": "state_normalization",
                    "concept": self._concept_payload(concept_id, available, include_evidence=True),
                    "states": state_chunk,
                }
                payload["package_id"] = stable_id("state_normalization", payload)
                packages.append(payload)
        return packages

    @staticmethod
    def _cluster_states(states: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
        if not states:
            return []
        by_id = {str(item["id"]): item for item in states}
        signatures = {
            state_id: "\n".join([
                str(item.get("text") or ""), str(item.get("current_normalized") or ""),
            ])
            for state_id, item in by_id.items()
        }
        index = CharNgramIndex(signatures)
        remaining = set(by_id)
        chunks: list[list[dict[str, Any]]] = []
        while remaining:
            anchor = min(remaining)
            excluded = set(by_id) - remaining | {anchor}
            neighbor_ids = [
                state_id for state_id, _ in index.query(
                    signatures[anchor], top_k=max(0, size - 1), exclude=excluded,
                )
            ]
            selected = [anchor, *neighbor_ids]
            if len(selected) < size:
                selected.extend(sorted(remaining - set(selected))[:size - len(selected)])
            selected = selected[:size]
            remaining.difference_update(selected)
            chunks.append([by_id[state_id] for state_id in selected])
        return chunks

    def _collect_objects(self) -> dict[str, dict[str, Any]]:
        aggregate: dict[str, dict[str, Any]] = {}
        state_counts: dict[str, Counter[str]] = defaultdict(Counter)
        context_counts: dict[str, Counter[tuple[str, str]]] = defaultdict(Counter)
        for rule in self.inputs.rules:
            all_states = [
                state for role in ("conditions", "antecedents", "consequents")
                for state in (rule.get(role) or [])
            ]
            for state in all_states:
                name = str(state.get("object") or "").strip()
                key = normalize_text(name)
                if not key:
                    continue
                item = aggregate.setdefault(key, {
                    "object_id": stable_id("object", key), "name": name, "frequency": 0,
                })
                item["frequency"] += 1
                state_counts[key][str(state.get("raw_state") or "")] += 1
                counterparts = [
                    f"{other.get('object', '')}|{other.get('raw_state', '')}"
                    for other in all_states if other.get("id") != state.get("id")
                ]
                for counterpart in counterparts[:2]:
                    context_counts[key][(str(rule.get("relation") or ""), counterpart)] += 1
        for key, item in aggregate.items():
            item["states"] = [value for value, _ in state_counts[key].most_common(5) if value]
            item["contexts"] = [
                {"relation": relation, "counterpart": counterpart}
                for (relation, counterpart), _ in context_counts[key].most_common(3)
            ]
        return aggregate

    def _concept_signature(self, row: dict[str, Any]) -> str:
        concept_id = str(row.get("concept_id") or "")
        relation_names: list[str] = []
        for relation in self.inputs.relations:
            child = str(relation.get("child_concept_id") or "")
            parent = str(relation.get("parent_concept_id") or "")
            if child == concept_id and parent in self.concepts:
                relation_names.append(str(self.concepts[parent].get("canonical_name") or ""))
            elif parent == concept_id and child in self.concepts:
                relation_names.append(str(self.concepts[child].get("canonical_name") or ""))
        return "\n".join(str(value) for value in [
            row.get("canonical_name") or "", *(row.get("aliases") or []),
            row.get("definition") or "", *relation_names,
        ] if value)

    def _concept_payload(
        self,
        concept_id: str,
        concepts: dict[str, dict[str, Any]] | None = None,
        *,
        include_evidence: bool = False,
    ) -> dict[str, Any]:
        row = (concepts or self.concepts)[concept_id]
        result = {
            "id": concept_id,
            "name": str(row.get("canonical_name") or ""),
            "aliases": list(row.get("aliases") or []),
            "definition": row.get("definition"),
        }
        if include_evidence and not result["definition"]:
            evidence = []
            for unit_id in row.get("evidence_ids") or []:
                unit = self.inputs.units.get(str(unit_id))
                if unit and unit.get("content"):
                    evidence.append(str(unit["content"]))
                if len(evidence) >= 3:
                    break
            result["evidence"] = evidence
        return result

    @staticmethod
    def _chunk(package_type: str, rows: list[dict[str, Any]], size: int, key: str) -> list[dict[str, Any]]:
        packages = []
        for offset in range(0, len(rows), size):
            payload = {"package_type": package_type, key: rows[offset:offset + size]}
            payload["package_id"] = stable_id(package_type, payload)
            packages.append(payload)
        return packages

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
