from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Any, Iterable

from .models import AlignmentStatus, ConceptType, MatchMethod, ProposalKind


def normalize_text(value: str) -> str:
    return "".join(str(value).split()).lower()


def stable_id(prefix: str, value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}_{hashlib.sha256(payload.encode()).hexdigest()[:16]}"


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


@dataclass(slots=True)
class MemorySnapshot:
    concepts: list[dict[str, Any]]
    relations: list[dict[str, Any]]
    version: str
    approved_memory_count: int

    @classmethod
    def build(
        cls,
        concepts: list[dict[str, Any]],
        relations: list[dict[str, Any]],
        reviewed_memory: list[dict[str, Any]] | None = None,
    ) -> MemorySnapshot:
        by_id = {str(row["concept_id"]): deepcopy(row) for row in concepts}
        relation_rows = [deepcopy(row) for row in relations]
        approved_count = 0
        for event in reviewed_memory or []:
            if str(event.get("status") or "").upper() != "APPROVED":
                continue
            approved_count += 1
            concept = event.get("concept")
            if isinstance(concept, dict) and concept.get("concept_id"):
                concept_type = str(concept.get("type") or "")
                if concept_type not in {ConceptType.OBJECT, ConceptType.STATE}:
                    raise ValueError("approved concept memory requires object/state type")
                by_id[str(concept["concept_id"])] = deepcopy(concept)
                continue
            kind = str(event.get("proposal_kind") or "")
            concept_id = str(event.get("concept_id") or "")
            if concept_id not in by_id:
                raise ValueError(f"reviewed memory references unknown concept: {concept_id}")
            if kind == ProposalKind.TYPE_REVIEW:
                concept_type = str(event.get("type") or "")
                if concept_type not in {ConceptType.OBJECT, ConceptType.STATE}:
                    raise ValueError("type review requires object/state type")
                by_id[concept_id]["type"] = concept_type
            elif kind == ProposalKind.ALIAS:
                alias = str(event.get("alias") or "").strip()
                if not alias:
                    raise ValueError("alias review requires alias")
                aliases = list(by_id[concept_id].get("aliases") or [])
                if normalize_text(alias) not in {normalize_text(value) for value in aliases}:
                    aliases.append(alias)
                by_id[concept_id]["aliases"] = aliases
            else:
                raise ValueError(f"unsupported approved memory event: {kind}")
        payload = {
            "concepts": sorted(by_id.values(), key=lambda row: str(row["concept_id"])),
            "relations": sorted(
                relation_rows,
                key=lambda row: (
                    str(row.get("child_concept_id") or ""),
                    str(row.get("type") or ""),
                    str(row.get("parent_concept_id") or ""),
                ),
            ),
        }
        return cls(
            concepts=payload["concepts"],
            relations=payload["relations"],
            version=stable_id("memory", payload),
            approved_memory_count=approved_count,
        )


class ConceptRegistry:
    """Typed, read-only concept registry backed by a frozen memory snapshot."""

    def __init__(self, snapshot: MemorySnapshot) -> None:
        self.snapshot = snapshot
        self.concepts = {str(row["concept_id"]): row for row in snapshot.concepts}
        if len(self.concepts) != len(snapshot.concepts):
            raise ValueError("concept_id must be unique")
        self.exact_index: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for concept_id, row in self.concepts.items():
            canonical = str(row.get("canonical_name") or "").strip()
            if not canonical:
                raise ValueError(f"concept requires canonical_name: {concept_id}")
            self.exact_index[normalize_text(canonical)].append((concept_id, MatchMethod.EXACT_NAME))
            for alias in row.get("aliases") or []:
                key = normalize_text(str(alias))
                if key and (concept_id, MatchMethod.EXACT_ALIAS) not in self.exact_index[key]:
                    self.exact_index[key].append((concept_id, MatchMethod.EXACT_ALIAS))
        self.parents: dict[str, list[str]] = defaultdict(list)
        for relation in snapshot.relations:
            if relation.get("type") != "IS_A":
                continue
            child = str(relation.get("child_concept_id") or "")
            parent = str(relation.get("parent_concept_id") or "")
            if child in self.concepts and parent in self.concepts and parent not in self.parents[child]:
                self.parents[child].append(parent)
        signatures = {concept_id: self._signature(concept_id) for concept_id in self.concepts}
        self.ngram = CharNgramIndex(signatures)
        self.lexical_terms: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for key, values in self.exact_index.items():
            for concept_id, _method in values:
                concept_type = str(self.concepts[concept_id].get("type") or "")
                if concept_type in {ConceptType.OBJECT, ConceptType.STATE}:
                    self.lexical_terms[concept_type].append((key, concept_id))
        for concept_type in self.lexical_terms:
            self.lexical_terms[concept_type].sort(key=lambda item: (-len(item[0]), item[0], item[1]))

    def __contains__(self, concept_id: str) -> bool:
        return concept_id in self.concepts

    def exact(self, text: str, *, concept_type: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for concept_id, method in self.exact_index.get(normalize_text(text), []):
            if concept_id in seen:
                continue
            row_type = str(self.concepts[concept_id].get("type") or "")
            if row_type and row_type != concept_type:
                continue
            result.append(self.payload(concept_id, match_method=method))
            seen.add(concept_id)
        return result

    def resolve_exact(self, text: str, *, concept_type: str) -> dict[str, Any]:
        matches = self.exact(text, concept_type=concept_type)
        typed = [row for row in matches if row.get("type") == concept_type]
        untyped = [row for row in matches if not row.get("type")]
        if len(typed) == 1 and not untyped:
            return {
                "status": AlignmentStatus.MATCHED,
                "concept_id": typed[0]["id"],
                "match_method": typed[0]["match_method"],
                "candidates": matches,
            }
        if not typed and len(untyped) == 1:
            return {
                "status": AlignmentStatus.TYPE_REVIEW,
                "concept_id": untyped[0]["id"],
                "match_method": untyped[0]["match_method"],
                "candidates": matches,
            }
        if matches:
            return {
                "status": AlignmentStatus.AMBIGUOUS,
                "concept_id": None,
                "match_method": MatchMethod.NONE,
                "candidates": matches,
            }
        return {
            "status": AlignmentStatus.EXPRESSION_ONLY,
            "concept_id": None,
            "match_method": MatchMethod.NONE,
            "candidates": [],
        }

    def candidates(self, text: str, *, concept_type: str, top_k: int = 6) -> list[dict[str, Any]]:
        result = self.exact(text, concept_type=concept_type)
        seen = {row["id"] for row in result}
        for concept_id, score in self.ngram.query(text, top_k=max(top_k * 4, top_k)):
            if concept_id in seen:
                continue
            row_type = str(self.concepts[concept_id].get("type") or "")
            if row_type and row_type != concept_type:
                continue
            result.append(self.payload(
                concept_id, match_method="FUZZY", score=score,
            ))
            seen.add(concept_id)
            if len(result) >= top_k:
                break
        return result

    def lexical_coverage(self, text: str, *, concept_type: str) -> float:
        normalized = normalize_text(text)
        if not normalized:
            return 0.0
        covered: set[int] = set()
        for term, _concept_id in self.lexical_terms.get(concept_type, []):
            start = normalized.find(term)
            while start >= 0:
                covered.update(range(start, start + len(term)))
                start = normalized.find(term, start + 1)
        return round(len(covered) / len(normalized), 4)

    def payload(
        self,
        concept_id: str,
        *,
        match_method: str | None = None,
        score: float | None = None,
    ) -> dict[str, Any]:
        row = self.concepts[concept_id]
        result = {
            "id": concept_id,
            "name": str(row.get("canonical_name") or ""),
            "aliases": list(row.get("aliases") or []),
            "type": row.get("type"),
            "definition": row.get("definition"),
            "evidence_ids": list(row.get("evidence_ids") or []),
            "parents": [
                {"id": parent, "name": str(self.concepts[parent].get("canonical_name") or "")}
                for parent in self.parents.get(concept_id, [])
            ],
        }
        if match_method:
            result["match_method"] = str(match_method)
        if score is not None:
            result["score"] = round(float(score), 6)
        return result

    def _signature(self, concept_id: str) -> str:
        row = self.concepts[concept_id]
        neighbors: list[str] = []
        for relation in self.snapshot.relations:
            child = str(relation.get("child_concept_id") or "")
            parent = str(relation.get("parent_concept_id") or "")
            if child == concept_id and parent in self.concepts:
                neighbors.append(str(self.concepts[parent].get("canonical_name") or ""))
            elif parent == concept_id and child in self.concepts:
                neighbors.append(str(self.concepts[child].get("canonical_name") or ""))
        return "\n".join(str(value) for value in [
            row.get("canonical_name") or "",
            *(row.get("aliases") or []),
            row.get("definition") or "",
            *(row.get("alignment_examples") or []),
            *neighbors,
        ] if value)
