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
    reviewed_concept_ids: frozenset[str]
    review_status_by_concept_id: dict[str, str]

    @classmethod
    def build(
        cls,
        concepts: list[dict[str, Any]],
        relations: list[dict[str, Any]],
        reviewed_memory: list[dict[str, Any]] | None = None,
    ) -> MemorySnapshot:
        by_id = {}
        for row in concepts:
            value = deepcopy(row)
            # Legacy inputs are unapproved until an explicit APPROVED memory event
            # admits them; a type field alone must not bypass the admission gate.
            value.setdefault("registration_status", "CANDIDATE")
            by_id[str(value["concept_id"])] = value
        relation_rows = [deepcopy(row) for row in relations]
        for relation in relation_rows:
            relation.setdefault("registration_status", "CANDIDATE")
        approved_count = 0
        reviewed_concept_ids: set[str] = set()
        review_status_by_concept_id: dict[str, str] = {}
        for event in reviewed_memory or []:
            concept_id = str(event.get("concept_id") or "")
            event_status = str(event.get("status") or "").upper()
            if concept_id and event_status:
                review_status_by_concept_id[concept_id] = event_status
            # DEFER is parked, not terminal: the concept may be re-admitted in
            # the final re-review pass after all tiers complete.
            if concept_id and str(event.get("status") or "").upper() != "DEFER":
                reviewed_concept_ids.add(concept_id)
            if str(event.get("status") or "").upper() != "APPROVED":
                continue
            approved_count += 1
            concept = event.get("concept")
            if isinstance(concept, dict) and concept.get("concept_id"):
                concept_type = str(concept.get("type") or "")
                if concept_type not in {ConceptType.OBJECT, ConceptType.STATE}:
                    raise ValueError("approved concept memory requires object/state type")
                approved = deepcopy(concept)
                approved["registration_status"] = "APPROVED"
                by_id[str(approved["concept_id"])] = approved
                relation_rows.extend(deepcopy(event.get("relations") or []))
                continue
            kind = str(event.get("proposal_kind") or "")
            if kind == ProposalKind.CONCEPT_MERGE:
                target_id = str(event.get("target_concept_id") or "")
                source = by_id.get(concept_id)
                if source is None and isinstance(event.get("source_concept"), dict):
                    source = deepcopy(event["source_concept"])
                if source is None or target_id not in by_id:
                    raise ValueError("concept merge requires existing source and target")
                if by_id[target_id].get("registration_status") != "APPROVED":
                    raise ValueError("concept merge target must be approved")
                target = by_id[target_id]
                occupied_terms = {
                    normalize_text(str(term))
                    for other_id, other in by_id.items()
                    if other_id not in {concept_id, target_id}
                    and other.get("registration_status") == "APPROVED"
                    for term in [
                        other.get("canonical_name") or "",
                        *(other.get("aliases") or []),
                    ]
                    if normalize_text(str(term))
                }
                aliases = [
                    *list(target.get("aliases") or []),
                    str(source.get("canonical_name") or ""),
                    *list(source.get("aliases") or []),
                    *list(event.get("aliases") or []),
                ]
                canonical_key = normalize_text(str(target.get("canonical_name") or ""))
                target["aliases"] = list(dict.fromkeys(
                    value for value in aliases
                    if value
                    and normalize_text(str(value)) != canonical_key
                    and normalize_text(str(value)) not in occupied_terms
                ))
                for field in ("evidence_ids", "source_package_ids", "source_seed_ids"):
                    target[field] = list(dict.fromkeys([
                        *list(target.get(field) or []),
                        *list(source.get(field) or []),
                        *list(event.get(field) or []),
                    ]))
                by_id.pop(concept_id, None)
                for relation in relation_rows:
                    if str(relation.get("child_concept_id") or "") == concept_id:
                        relation["child_concept_id"] = target_id
                    if str(relation.get("parent_concept_id") or "") == concept_id:
                        relation["parent_concept_id"] = target_id
                continue
            if concept_id not in by_id:
                raise ValueError(f"reviewed memory references unknown concept: {concept_id}")
            if kind == ProposalKind.TYPE_REVIEW:
                concept_type = str(event.get("type") or "")
                if concept_type not in {ConceptType.OBJECT, ConceptType.STATE}:
                    raise ValueError("type review requires object/state type")
                by_id[concept_id]["type"] = concept_type
                # An APPROVED type review is an explicit approval: the concept
                # must resolve as registered, not be re-proposed every epoch.
                by_id[concept_id]["registration_status"] = "APPROVED"
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
            reviewed_concept_ids=frozenset(reviewed_concept_ids),
            review_status_by_concept_id=review_status_by_concept_id,
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
        self.missing_relation_endpoint_count = 0
        self.parents: dict[str, list[str]] = defaultdict(list)
        neighbors: dict[str, list[str]] = defaultdict(list)
        for relation in snapshot.relations:
            if relation.get("registration_status") != "APPROVED":
                continue
            child = str(relation.get("child_concept_id") or "")
            parent = str(relation.get("parent_concept_id") or "")
            if relation.get("type") == "IS_A":
                if child not in self.concepts or parent not in self.concepts:
                    self.missing_relation_endpoint_count += 1
                    continue
                if parent not in self.parents[child]:
                    self.parents[child].append(parent)
            if child in self.concepts and parent in self.concepts:
                if parent not in neighbors[child]:
                    neighbors[child].append(parent)
                if child not in neighbors[parent]:
                    neighbors[parent].append(child)
        signatures = {
            concept_id: self._signature(concept_id, neighbors) for concept_id in self.concepts
        }
        self.ngram = CharNgramIndex(signatures)
        name_signatures = {
            concept_id: "\n".join(str(value) for value in [
                row.get("canonical_name") or "", *(row.get("aliases") or []),
            ] if value)
            for concept_id, row in self.concepts.items()
        }
        self.name_ngram = CharNgramIndex(name_signatures)
        self.lexical_terms: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for key, values in self.exact_index.items():
            for concept_id, _method in values:
                concept_type = str(self.concepts[concept_id].get("type") or "")
                if (
                    concept_type in {ConceptType.OBJECT, ConceptType.STATE}
                    and self.is_approved(concept_id)
                ):
                    self.lexical_terms[concept_type].append((key, concept_id))
        for concept_type in self.lexical_terms:
            self.lexical_terms[concept_type].sort(key=lambda item: (-len(item[0]), item[0], item[1]))
        self.candidate_terms = sorted(
            (
                (term, concept_id)
                for term, rows in self.exact_index.items()
                for concept_id, _method in rows
                if self.is_approved(concept_id)
            ),
            key=lambda item: (-len(item[0]), item[0], item[1]),
        )

    def __contains__(self, concept_id: str) -> bool:
        return concept_id in self.concepts

    def is_approved(self, concept_id: str) -> bool:
        return (
            concept_id in self.concepts
            and self.concepts[concept_id].get("registration_status") == "APPROVED"
        )

    def registered_term_owners(self, text: str) -> list[str]:
        return sorted({
            concept_id for concept_id, _method in self.exact_index.get(normalize_text(text), [])
            if self.is_approved(concept_id)
            and str(self.concepts[concept_id].get("type") or "") == ConceptType.OBJECT
        })

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
        approved = [row for row in matches if row.get("registration_status") == "APPROVED"]
        candidates = [row for row in matches if row.get("registration_status") != "APPROVED"]
        typed = [row for row in approved if row.get("type") == concept_type]
        untyped = [row for row in approved if not row.get("type")]
        # An approved concept wins even when an unapproved candidate shares the
        # exact name; the candidate stays visible via the "candidates" payload.
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
        if not approved and len(candidates) == 1:
            candidate_id = str(candidates[0]["id"])
            if self.snapshot.review_status_by_concept_id.get(candidate_id) == "REJECT":
                return {
                    "status": AlignmentStatus.REJECTED,
                    "concept_id": None,
                    "candidate_concept_id": candidate_id,
                    "match_method": MatchMethod.NONE,
                    "candidates": matches,
                }
            return {
                "status": AlignmentStatus.PROPOSED,
                "concept_id": candidates[0]["id"],
                "match_method": candidates[0]["match_method"],
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
            "status": "NO_MATCH",
            "concept_id": None,
            "match_method": MatchMethod.NONE,
            "candidates": [],
        }

    def candidates(self, text: str, *, concept_type: str, top_k: int = 6) -> list[dict[str, Any]]:
        result = [
            row for row in self.exact(text, concept_type=concept_type)
            if row.get("registration_status") == "APPROVED"
        ]
        seen = {row["id"] for row in result}
        remaining = max(0, top_k - len(result))
        if not remaining:
            return result
        name_slots = min(remaining, max(1, round(top_k * 0.625)))
        initial_name_count = len(result)
        self._append_contained_name_candidates(
            result, seen, text, concept_type=concept_type, limit=name_slots,
        )
        self._append_fuzzy_candidates(
            result, seen, self.name_ngram.query(text, top_k=max(top_k * 4, top_k)),
            concept_type=concept_type, limit=max(0, name_slots - (len(result) - initial_name_count)),
            match_method="FUZZY_NAME",
        )
        self._append_fuzzy_candidates(
            result, seen, self.ngram.query(text, top_k=max(top_k * 4, top_k)),
            concept_type=concept_type, limit=top_k,
            match_method="FUZZY_CONTEXT",
        )
        return result

    def identity_candidates(self, text: str, *, top_k: int = 8) -> list[dict[str, Any]]:
        """Recall registered object concepts for identity comparison."""

        normalized = normalize_text(text)
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for concept_id, method in self.exact_index.get(normalized, []):
            if (
                not self.is_approved(concept_id)
                or str(self.concepts[concept_id].get("type") or "") != ConceptType.OBJECT
                or concept_id in seen
            ):
                continue
            result.append(self.payload(concept_id, match_method=method, score=1.0))
            seen.add(concept_id)
        if len(result) >= top_k:
            return result[:top_k]
        for index, match_method in (
            (self.name_ngram, "FUZZY_NAME"),
            (self.ngram, "FUZZY_CONTEXT"),
        ):
            for concept_id, score in index.query(text, top_k=max(top_k * 4, top_k)):
                if (
                    concept_id in seen
                    or not self.is_approved(concept_id)
                    or str(self.concepts[concept_id].get("type") or "") != ConceptType.OBJECT
                ):
                    continue
                result.append(self.payload(concept_id, match_method=match_method, score=score))
                seen.add(concept_id)
                if len(result) >= top_k:
                    return result
        return result

    def _append_contained_name_candidates(
        self,
        result: list[dict[str, Any]],
        seen: set[str],
        text: str,
        *,
        concept_type: str,
        limit: int,
    ) -> None:
        normalized = normalize_text(text)
        added = 0
        for term, concept_id in self.candidate_terms:
            if added >= limit:
                break
            if term not in normalized or concept_id in seen:
                continue
            if not self.is_approved(concept_id):
                continue
            row_type = str(self.concepts[concept_id].get("type") or "")
            if row_type and row_type != concept_type:
                continue
            result.append(self.payload(concept_id, match_method="CONTAINED_NAME", score=float(len(term))))
            seen.add(concept_id)
            added += 1

    def _append_fuzzy_candidates(
        self,
        result: list[dict[str, Any]],
        seen: set[str],
        candidates: list[tuple[str, float]],
        *,
        concept_type: str,
        limit: int,
        match_method: str,
    ) -> None:
        added = 0
        for concept_id, score in candidates:
            if added >= limit or len(result) >= limit and match_method == "FUZZY_CONTEXT":
                break
            if concept_id in seen:
                continue
            if not self.is_approved(concept_id):
                continue
            row_type = str(self.concepts[concept_id].get("type") or "")
            if row_type and row_type != concept_type:
                continue
            result.append(self.payload(concept_id, match_method=match_method, score=score))
            seen.add(concept_id)
            added += 1

    def lexical_coverage(self, text: str, *, concept_type: str) -> float:
        normalized = normalize_text(text)
        if not normalized:
            return 0.0
        covered: set[int] = set()
        for term, _concept_id in self.lexical_terms.get(concept_type, []):
            if len(covered) == len(normalized):
                break
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
            "registration_status": row.get("registration_status", "CANDIDATE"),
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

    def _signature(self, concept_id: str, neighbors: dict[str, list[str]]) -> str:
        row = self.concepts[concept_id]
        return "\n".join(str(value) for value in [
            row.get("canonical_name") or "",
            *(row.get("aliases") or []),
            row.get("definition") or "",
            *(row.get("alignment_examples") or []),
            *(str(self.concepts[neighbor].get("canonical_name") or "") for neighbor in neighbors.get(concept_id, [])),
        ] if value)
