from __future__ import annotations

from collections import Counter, defaultdict, deque
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import yaml

from constella.context_builder.llm_client import LLMClient

from .lifecycle import LifecycleState, audit_concept_library, rank_by_occurrence
from .models import ConceptType, ProposalKind
from .packages import AlignmentInputs
from .registry import ConceptRegistry, MemorySnapshot, normalize_text, stable_id


_DECISIONS = {"APPROVE", "MERGE", "DEFER", "REJECT"}
_CHECKS = {
    "stable_kind", "not_instance_or_parameter", "single_identity",
    "evidence_sufficient", "type_clear", "not_duplicate",
}
_RELATION_TYPES = {"IS_A", "PART_OF"}
_GENERIC_RELATION_NAMES = {
    "头部", "尾部", "上部", "下部", "内部", "外部", "类型", "种类", "形式", "部分",
}


def build_initial_pending_concepts(
    inputs: AlignmentInputs,
    memory: MemorySnapshot,
) -> list[dict[str, Any]]:
    """Rank every extracted, unregistered concept by rule-object occurrence."""

    object_counts: Counter[str] = Counter()
    object_variants: dict[str, Counter[str]] = defaultdict(Counter)
    object_sources: dict[str, set[str]] = defaultdict(set)
    for rule in inputs.rules:
        for side in ("conditions", "antecedents", "consequents"):
            for state in rule.get(side) or []:
                raw_object = str(state.get("object") or "").strip()
                key = normalize_text(raw_object)
                if not key:
                    continue
                object_counts[key] += 1
                object_variants[key][raw_object] += 1
                object_sources[key].add(str(state.get("id") or ""))

    concepts_by_id = {str(row["concept_id"]): row for row in memory.concepts}
    relation_hints: dict[str, list[dict[str, Any]]] = defaultdict(list)
    relation_evidence: dict[str, set[str]] = defaultdict(set)
    for relation in inputs.relations:
        child_id = str(relation.get("child_concept_id") or "")
        parent_id = str(relation.get("parent_concept_id") or "")
        relation_type = str(relation.get("type") or "")
        if relation_type not in _RELATION_TYPES:
            continue
        for concept_id, other_id, direction in (
            (child_id, parent_id, "OUTGOING"),
            (parent_id, child_id, "INCOMING"),
        ):
            other = concepts_by_id.get(other_id)
            if concept_id not in concepts_by_id or not other:
                continue
            evidence_ids = [str(value) for value in relation.get("evidence_ids") or []]
            relation_evidence[concept_id].update(evidence_ids)
            relation_hints[concept_id].append({
                "type": relation_type,
                "direction": direction,
                "other_concept_id": other_id,
                "other_canonical_name": str(other.get("canonical_name") or ""),
                "other_definition": other.get("definition"),
                "evidence_ids": evidence_ids,
            })

    rows: list[dict[str, Any]] = []
    for concept in memory.concepts:
        if concept.get("registration_status") == "APPROVED":
            continue
        terms = {
            normalize_text(str(value)) for value in [
                concept.get("canonical_name"), *(concept.get("aliases") or []),
            ] if normalize_text(str(value or ""))
        }
        occurrence_count = sum(object_counts[term] for term in terms)
        source_ids = sorted({value for term in terms for value in object_sources[term] if value})
        variants: Counter[str] = Counter()
        for term in terms:
            variants.update(object_variants[term])
        concept_id = str(concept["concept_id"])
        evidence_ids = list(dict.fromkeys([
            *list(concept.get("evidence_ids") or []),
            *sorted(relation_evidence[concept_id]),
        ]))
        evidence_source = {**concept, "evidence_ids": evidence_ids}
        rows.append({
            "candidate_id": concept_id,
            "concept_id": concept_id,
            "canonical_name": str(concept.get("canonical_name") or ""),
            "aliases": list(concept.get("aliases") or []),
            "definition": concept.get("definition"),
            "definition_type": concept.get("definition_type"),
            "suggested_type": concept.get("type"),
            "evidence_ids": evidence_ids,
            "source_package_ids": list(concept.get("source_package_ids") or []),
            "source_seed_ids": list(concept.get("source_seed_ids") or []),
            "origin_depth": int(concept.get("origin_depth") or 0),
            "occurrence_count": occurrence_count,
            "source_state_ids": source_ids[:50],
            "raw_object_variants": [
                {"text": text, "count": count} for text, count in variants.most_common(12)
            ],
            "evidence": recall_concept_evidence(evidence_source, inputs.units),
            "catalog_relation_hints": relation_hints[concept_id],
            "lifecycle_state": LifecycleState.PENDING_CONCEPT,
            "candidate_origin": "EXTRACTED_CONCEPT",
        })
    return rank_by_occurrence(rows, identity_field="candidate_id")


def recall_concept_evidence(
    concept: dict[str, Any],
    units: dict[str, dict[str, Any]],
    *,
    limit: int = 24,
) -> list[dict[str, Any]]:
    """Recall book units, preferring explicit evidence and then name hits."""

    explicit = {str(value) for value in concept.get("evidence_ids") or []}
    terms = [
        normalize_text(str(value)) for value in [
            concept.get("canonical_name"), *(concept.get("aliases") or []),
        ] if normalize_text(str(value or ""))
    ]
    scored: list[tuple[tuple[int, int, int, str], dict[str, Any]]] = []
    for unit_id, unit in units.items():
        content = str(unit.get("content") or "")
        normalized = normalize_text(content)
        hit_count = sum(normalized.count(term) for term in terms if term)
        if unit_id not in explicit and not hit_count:
            continue
        score = (
            1 if unit_id in explicit else 0,
            hit_count,
            1 if unit.get("type") in {"text", "title"} else 0,
            str(unit_id),
        )
        scored.append((score, {
            "evidence_id": str(unit_id),
            "text": content,
            "section_path": list((unit.get("attributes") or {}).get("section_path") or []),
            "page": (unit.get("source") or {}).get("page"),
            "explicit": unit_id in explicit,
        }))
    scored.sort(key=lambda row: (-row[0][0], -row[0][1], -row[0][2], row[0][3]))
    return [row[1] for row in scored[:limit]]


def build_pending_concepts_from_proposals(
    proposal_rows: list[dict[str, Any]],
    inputs: AlignmentInputs,
    memory: MemorySnapshot,
    *,
    reviewed_concept_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Turn unresolved alignment proposals into complete, evidence-bound candidates."""

    reviewed = reviewed_concept_ids or set()
    concepts = {str(row["concept_id"]): row for row in memory.concepts}
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    supported_kinds = {
        ProposalKind.OBJECT_CONCEPT,
        ProposalKind.CONCEPT_APPROVAL, ProposalKind.TYPE_REVIEW,
    }
    for proposal in proposal_rows:
        if proposal.get("proposal_kind") not in supported_kinds:
            continue
        name = str(proposal.get("canonical_name") or "").strip()
        concept_type = str(proposal.get("concept_type") or "")
        if not name or concept_type != ConceptType.OBJECT:
            continue
        concept_id = str(proposal.get("concept_id") or "") or stable_id(
            "concept", {"name": normalize_text(name), "type": concept_type},
        )
        if concept_id in reviewed or any(
            str(row["concept_id"]) == concept_id and row.get("registration_status") == "APPROVED"
            for row in memory.concepts
        ):
            continue
        key = (concept_id, concept_type)
        source = concepts.get(concept_id, {})
        row = grouped.setdefault(key, {
            "candidate_id": concept_id,
            "concept_id": concept_id,
            "canonical_name": str(source.get("canonical_name") or name),
            "aliases": list(source.get("aliases") or []),
            "definition": source.get("definition"),
            "definition_type": source.get("definition_type") or "contextual_draft",
            "suggested_type": concept_type,
            "evidence_ids": set(source.get("evidence_ids") or []),
            "source_package_ids": set(source.get("source_package_ids") or []),
            "source_seed_ids": list(source.get("source_seed_ids") or []),
            "origin_depth": int(source.get("origin_depth") or 0),
            "occurrence_count": 0,
            "source_state_ids": set(),
            "raw_object_variants": Counter(),
            "lifecycle_state": LifecycleState.PENDING_CONCEPT,
            "candidate_origin": "UNPROCESSED_OBJECT",
        })
        row["occurrence_count"] += int(proposal.get("support") or 0)
        row["source_state_ids"].update(proposal.get("source_state_ids") or [])
        row["source_package_ids"].update(proposal.get("context_package_ids") or [])
        for value in proposal.get("raw_expressions") or []:
            row["raw_object_variants"][str(value)] += 1

    result: list[dict[str, Any]] = []
    for row in grouped.values():
        for package_id in row["source_package_ids"]:
            package = inputs.context_packages.get(str(package_id)) or {}
            row["evidence_ids"].update(package.get("core_unit_ids") or [])
            row["evidence_ids"].update(package.get("support_unit_ids") or [])
        concept = {
            "canonical_name": row["canonical_name"],
            "aliases": row["aliases"],
            "evidence_ids": sorted(row["evidence_ids"]),
        }
        result.append({
            **{key: value for key, value in row.items() if key not in {
                "evidence_ids", "source_package_ids", "source_state_ids", "raw_object_variants",
            }},
            "evidence_ids": sorted(row["evidence_ids"]),
            "source_package_ids": sorted(row["source_package_ids"]),
            "source_state_ids": sorted(row["source_state_ids"])[:50],
            "raw_object_variants": [
                {"text": text, "count": count}
                for text, count in row["raw_object_variants"].most_common(12)
            ],
            "evidence": recall_concept_evidence(concept, inputs.units),
        })
    return rank_by_occurrence(result, identity_field="candidate_id")


class SerialConceptAdmissionRunner:
    """Admit one concept at a time against the latest registered memory."""

    def __init__(
        self,
        models: dict[str, Any],
        model_key: str,
        prompt_path: str | Path,
        output_dir: str | Path,
        *,
        client=None,
    ) -> None:
        self.models = models
        self.model_key = model_key
        self.output_dir = Path(output_dir)
        self.client = client or LLMClient(models)
        self.prompt = yaml.safe_load(Path(prompt_path).read_text(encoding="utf-8"))
        if not isinstance(self.prompt, dict) or not {"id", "version", "system"} <= set(self.prompt):
            raise ValueError("invalid serial concept admission prompt")

    def run(
        self,
        candidates: list[dict[str, Any]],
        *,
        concepts: list[dict[str, Any]],
        relations: list[dict[str, Any]],
        reviewed_memory: list[dict[str, Any]] | None = None,
        refresh: bool = False,
        limit: int | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        base_events = list(reviewed_memory or [])
        selected_candidates = candidates[:limit] if limit is not None else candidates
        checkpoint_path = self.output_dir / "admission_checkpoint.json"
        checkpoint_key = stable_id("serial_admission_checkpoint", {
            "candidate_ids": [str(row["concept_id"]) for row in selected_candidates],
            "concept_ids": [str(row["concept_id"]) for row in concepts],
            "relation_ids": [str(row.get("relation_id") or "") for row in relations],
            "base_event_ids": [str(row.get("event_id") or "") for row in base_events],
            "prompt_id": self.prompt["id"],
            "prompt_version": str(self.prompt["version"]),
            "model": self.models[self.model_key]["model"],
        })
        saved = self._load_checkpoint(checkpoint_path, checkpoint_key) if not refresh else None
        if saved:
            events = base_events + list(saved["new_events"])
            concept_rows = list(saved["concept_rows"])
            queue = deque(saved["queue"])
            processed_ids = set(saved["processed_ids"])
            reviews = list(saved["reviews"])
            generated_count = int(saved["generated_count"])
        else:
            events = list(base_events)
            concept_rows = [dict(row) for row in concepts]
            queue = deque(selected_candidates)
            processed_ids = set()
            reviews = []
            generated_count = 0
        queued_names = {
            normalize_text(str(row.get("canonical_name") or ""))
            for row in concept_rows
            if normalize_text(str(row.get("canonical_name") or ""))
        }
        while queue:
            candidate = queue.popleft()
            candidate_id = str(candidate["concept_id"])
            if candidate_id in processed_ids:
                continue
            memory = MemorySnapshot.build(concept_rows, relations, events)
            if candidate_id not in {str(row["concept_id"]) for row in memory.concepts}:
                processed_ids.add(candidate_id)
                continue
            registry = ConceptRegistry(memory)
            package = self._package(candidate, registry, memory.version)
            result = None if refresh else self._load_cached(package)
            if result is None:
                result = self._process(package)
            review, event, generated = self._review(package, result, registry)
            reviews.append(review)
            processed_ids.add(candidate_id)
            if event is not None:
                events.append(event)
            for row in generated:
                key = normalize_text(str(row.get("canonical_name") or ""))
                if not key or key in queued_names:
                    continue
                queued_names.add(key)
                queue.append(row)
                concept_rows.append({
                    "concept_id": row["concept_id"],
                    "canonical_name": row["canonical_name"],
                    "aliases": row.get("aliases") or [],
                    "definition": row.get("definition"),
                    "definition_type": row.get("definition_type"),
                    "evidence_ids": row.get("evidence_ids") or [],
                    "source_package_ids": row.get("source_package_ids") or [],
                    "source_seed_ids": row.get("source_seed_ids") or [],
                    "origin_depth": row.get("origin_depth", 1),
                    "registration_status": "CANDIDATE",
                })
                generated_count += 1
            self._atomic_json(checkpoint_path, {
                "checkpoint_key": checkpoint_key,
                "new_events": events[len(base_events):],
                "concept_rows": concept_rows,
                "queue": list(queue),
                "processed_ids": sorted(processed_ids),
                "reviews": reviews,
                "generated_count": generated_count,
            })

        final_memory = MemorySnapshot.build(concept_rows, relations, events)
        report = {
            "input_candidate_count": len(candidates),
            "processed_candidate_count": len(reviews),
            "generated_pending_concept_count": generated_count,
            "approved_count": sum(row["decision"] == "APPROVE" and row["accepted"] for row in reviews),
            "merged_count": sum(row["decision"] == "MERGE" and row["accepted"] for row in reviews),
            "deferred_count": sum(row["decision"] == "DEFER" for row in reviews),
            "rejected_count": sum(row["decision"] == "REJECT" for row in reviews),
            "failed_count": sum(row["decision"] == "FAILED" for row in reviews),
            "reviewed_memory_event_count": len(events),
            "final_memory_version": final_memory.version,
            "library_audit": audit_concept_library(final_memory),
        }
        return reviews, events, report

    @staticmethod
    def _load_checkpoint(path: Path, checkpoint_key: str) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        required = {
            "new_events", "concept_rows", "queue", "processed_ids", "reviews",
            "generated_count",
        }
        if value.get("checkpoint_key") != checkpoint_key or not required <= set(value):
            return None
        return value

    def _package(
        self,
        candidate: dict[str, Any],
        registry: ConceptRegistry,
        memory_version: str,
    ) -> dict[str, Any]:
        recalled = registry.identity_candidates(
            "\n".join(str(value) for value in [
                candidate.get("canonical_name") or "",
                *(candidate.get("aliases") or []),
                candidate.get("definition") or "",
                *(row.get("text") or "" for row in candidate.get("raw_object_variants") or []),
            ] if value),
            top_k=10,
        )
        seen = {str(row["id"]) for row in recalled}
        for hint in candidate.get("catalog_relation_hints") or []:
            other_id = str(hint.get("other_concept_id") or "")
            if other_id and other_id not in seen and registry.is_approved(other_id):
                recalled.append(registry.payload(other_id, match_method="CATALOG_RELATION_HINT"))
                seen.add(other_id)
        payload = {
            "package_type": "serial_concept_admission",
            "memory_version": memory_version,
            "candidate": candidate,
            "registered_candidates": recalled,
        }
        payload["package_id"] = stable_id("serial_concept_admission", payload)
        return payload

    def _process(self, package: dict[str, Any]) -> dict[str, Any]:
        messages = [
            {"role": "system", "content": self.prompt["system"]},
            {"role": "user", "content": json.dumps(package, ensure_ascii=False)},
        ]
        errors: list[str] = []
        for attempt in range(1, 3):
            content: str | None = None
            try:
                response = self.client.complete(
                    self.model_key, messages, response_format={"type": "json_object"},
                    prompt_id=self.prompt["id"], prompt_version=str(self.prompt["version"]),
                    input_unit_ids=[
                        str(row["evidence_id"])
                        for row in package["candidate"].get("evidence") or []
                    ],
                    max_tokens=int(self.prompt.get("max_tokens", 3200)),
                )
                content = response["choices"][0]["message"]["content"]
                output = json.loads(content)
                self._validate(package, output)
                result = {
                    "package_id": package["package_id"], "status": "success",
                    "attempt_count": attempt, "input_fingerprint": self._fingerprint(package),
                    "served_model": response.get("model"), "output": output,
                }
                self._store(package, result)
                return result
            except Exception as error:
                errors.append(f"{type(error).__name__}: {error}")
                if attempt == 1:
                    if content is not None:
                        messages.append({"role": "assistant", "content": content})
                    messages.append({
                        "role": "user",
                        "content": f"输出不符合协议：{error}。只返回修正后的JSON。",
                    })
        result = {
            "package_id": package["package_id"], "status": "failed", "attempt_count": 2,
            "input_fingerprint": self._fingerprint(package), "errors": errors,
        }
        self._store(package, result)
        return result

    @staticmethod
    def _validate(package: dict[str, Any], output: dict[str, Any]) -> None:
        if not isinstance(output, dict) or output.get("decision") not in _DECISIONS:
            raise ValueError("invalid decision")
        if str(output.get("concept_id") or "") != str(package["candidate"]["concept_id"]):
            raise ValueError("decision concept_id mismatch")
        decision = output["decision"]
        if output.get("confidence") not in {"HIGH", "MEDIUM", "LOW"}:
            raise ValueError("invalid model confidence")
        checks = output.get("boundary_checks")
        if not isinstance(checks, dict) or set(checks) != _CHECKS:
            raise ValueError("invalid boundary checks")
        if not all(isinstance(checks[key], bool) for key in _CHECKS):
            raise ValueError("boundary checks must be booleans")
        if not isinstance(output.get("reason"), str) or not output["reason"].strip():
            raise ValueError("reason is required")
        for field in ("aliases", "evidence_ids", "relations", "missing_relation_concepts"):
            if not isinstance(output.get(field), list):
                raise ValueError(f"{field} must be a list")
        registered_ids = {str(row["id"]) for row in package["registered_candidates"]}
        target = output.get("target_concept_id")
        if decision == "MERGE" and str(target or "") not in registered_ids:
            raise ValueError("merge target must be a supplied registered candidate")
        if decision != "MERGE" and target is not None:
            raise ValueError("only merge may select target_concept_id")
        selected_type = output.get("selected_type")
        if decision == "APPROVE" and selected_type not in {ConceptType.OBJECT, ConceptType.STATE}:
            raise ValueError("approval requires object/state selected_type")
        if decision != "APPROVE" and selected_type is not None:
            raise ValueError("non-approval selected_type must be null")
        allowed_evidence = {
            str(row["evidence_id"]) for row in package["candidate"].get("evidence") or []
        }
        if not set(output.get("evidence_ids") or []) <= allowed_evidence:
            raise ValueError("decision cites evidence outside supplied book recall")
        if decision == "APPROVE":
            if not isinstance(output.get("canonical_name"), str) or not output["canonical_name"].strip():
                raise ValueError("approval requires canonical_name")
            if not isinstance(output.get("definition"), str) or not output["definition"].strip():
                raise ValueError("approval requires evidence-grounded definition")
            if not 1 <= len(output["evidence_ids"]) <= 8:
                raise ValueError("approval requires one to eight direct evidence ids")
        if decision == "MERGE" and not 1 <= len(output["evidence_ids"]) <= 8:
            raise ValueError("merge requires one to eight direct evidence ids")
        if len(output["relations"]) > 8 or len(output["missing_relation_concepts"]) > 8:
            raise ValueError("at most eight relations or missing concepts are allowed")
        for relation in output.get("relations") or []:
            if relation.get("type") not in _RELATION_TYPES:
                raise ValueError("invalid registered relation type")
            if relation.get("direction") not in {"OUTGOING", "INCOMING"}:
                raise ValueError("invalid registered relation direction")
            if str(relation.get("target_concept_id") or "") not in registered_ids:
                raise ValueError("relation target must be registered and supplied")
            if not set(relation.get("evidence_ids") or []) <= allowed_evidence:
                raise ValueError("relation cites unknown evidence")
            if not 1 <= len(relation.get("evidence_ids") or []) <= 4:
                raise ValueError("relation requires one to four direct evidence ids")
        for missing in output.get("missing_relation_concepts") or []:
            if not isinstance(missing, dict) or not str(missing.get("canonical_name") or "").strip():
                raise ValueError("missing relation concept requires canonical_name")
            if missing.get("type") not in {None, ConceptType.OBJECT, ConceptType.STATE}:
                raise ValueError("invalid missing relation concept type")
            if not set(missing.get("evidence_ids") or []) <= allowed_evidence:
                raise ValueError("missing relation concept cites unknown evidence")
            if not 1 <= len(missing.get("evidence_ids") or []) <= 4:
                raise ValueError("missing relation concept requires one to four direct evidence ids")

    def _review(
        self,
        package: dict[str, Any],
        result: dict[str, Any],
        registry: ConceptRegistry,
    ) -> tuple[dict[str, Any], dict[str, Any] | None, list[dict[str, Any]]]:
        candidate = package["candidate"]
        if result.get("status") != "success":
            return ({
                "concept_id": candidate["concept_id"], "canonical_name": candidate["canonical_name"],
                "decision": "FAILED", "accepted": False, "errors": result.get("errors") or [],
                "memory_version": package["memory_version"],
            }, None, [])
        decision = result["output"]
        accepted, gate_reason = self._post_gate(decision, registry)
        review = {
            "review_id": stable_id("serial_concept_review", {
                "package_id": package["package_id"], "decision": decision,
            }),
            "concept_id": candidate["concept_id"], "canonical_name": candidate["canonical_name"],
            "occurrence_count": candidate.get("occurrence_count", 0),
            "occurrence_rank": candidate.get("occurrence_rank"),
            "rank_confidence": candidate.get("rank_confidence"),
            "memory_version": package["memory_version"], "decision": decision["decision"],
            "accepted": accepted, "gate_reason": gate_reason, "model_decision": decision,
            "package_id": package["package_id"], "prompt_id": self.prompt["id"],
            "prompt_version": str(self.prompt["version"]),
            "configured_model": self.models[self.model_key]["model"],
        }
        event = self._event(candidate, review) if accepted else None
        generated = self._generated_candidates(candidate, review) if accepted else []
        return review, event, generated

    @staticmethod
    def _post_gate(decision: dict[str, Any], registry: ConceptRegistry) -> tuple[bool, str]:
        kind = decision["decision"]
        if kind in {"DEFER", "REJECT"}:
            return False, f"model_{kind.lower()}"
        if decision["confidence"] != "HIGH":
            return False, "admission_requires_high_model_confidence"
        if kind == "APPROVE" and not all(decision["boundary_checks"].values()):
            return False, "approval_requires_all_boundary_checks"
        if kind == "APPROVE":
            terms = [decision.get("canonical_name"), *(decision.get("aliases") or [])]
            if any(registry.registered_term_owners(str(term or "")) for term in terms if term):
                return False, "registered_name_or_alias_collision_requires_merge"
        if kind == "MERGE":
            target = str(decision.get("target_concept_id") or "")
            required = {
                "stable_kind", "not_instance_or_parameter", "single_identity",
                "evidence_sufficient", "type_clear",
            }
            if not registry.is_approved(target):
                return False, "merge_target_not_registered"
            if not all(decision["boundary_checks"][key] for key in required):
                return False, "merge_requires_valid_concept_boundaries"
        return True, "serial_model_and_deterministic_gate_passed"

    @staticmethod
    def _event(candidate: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
        decision = review["model_decision"]
        common = {
            "event_id": stable_id("memory_event", review["review_id"]),
            "status": "APPROVED", "concept_id": candidate["concept_id"],
            "base_memory_version": review["memory_version"],
            "approval_mode": "SERIAL_MODEL_GATE", "reason": decision["reason"],
            "confidence": decision["confidence"],
            "boundary_checks": decision["boundary_checks"],
            "source_review_id": review["review_id"],
        }
        if decision["decision"] == "MERGE":
            return {
                **common, "proposal_kind": ProposalKind.CONCEPT_MERGE,
                "target_concept_id": decision["target_concept_id"],
                "aliases": list(decision.get("aliases") or []),
                "evidence_ids": list(decision.get("evidence_ids") or []),
                "source_package_ids": list(candidate.get("source_package_ids") or []),
                "source_seed_ids": list(candidate.get("source_seed_ids") or []),
                "source_concept": {
                    "concept_id": candidate["concept_id"],
                    "canonical_name": candidate["canonical_name"],
                    "aliases": list(candidate.get("aliases") or []),
                    "evidence_ids": list(candidate.get("evidence_ids") or []),
                    "source_package_ids": list(candidate.get("source_package_ids") or []),
                    "source_seed_ids": list(candidate.get("source_seed_ids") or []),
                },
            }
        concept = {
            "concept_id": candidate["concept_id"],
            "canonical_name": str(decision.get("canonical_name") or candidate["canonical_name"]),
            "aliases": list(dict.fromkeys(decision.get("aliases") or candidate.get("aliases") or [])),
            "definition": decision.get("definition") or candidate.get("definition"),
            "definition_type": "reviewed", "type": decision["selected_type"],
            "registration_status": "APPROVED",
            "evidence_ids": list(decision.get("evidence_ids") or candidate.get("evidence_ids") or []),
            "source_package_ids": list(candidate.get("source_package_ids") or []),
            "source_seed_ids": list(candidate.get("source_seed_ids") or []),
            "origin_depth": int(candidate.get("origin_depth") or 0),
        }
        relations = []
        for row in decision.get("relations") or []:
            if row["direction"] == "OUTGOING":
                child_id, parent_id = candidate["concept_id"], row["target_concept_id"]
            else:
                child_id, parent_id = row["target_concept_id"], candidate["concept_id"]
            relations.append({
                "relation_id": stable_id("relation", {
                    "child": child_id, "type": row["type"], "parent": parent_id,
                }),
                "child_concept_id": child_id, "type": row["type"],
                "parent_concept_id": parent_id, "directness": "direct",
                "evidence_ids": list(row.get("evidence_ids") or []),
                "source_package_ids": list(candidate.get("source_package_ids") or []),
                "audit_status": "reviewed",
                "registration_status": "APPROVED",
            })
        return {
            **common, "proposal_kind": ProposalKind.CONCEPT_APPROVAL,
            "type": decision["selected_type"], "concept": concept, "relations": relations,
        }

    @staticmethod
    def _generated_candidates(
        candidate: dict[str, Any], review: dict[str, Any],
    ) -> list[dict[str, Any]]:
        result = []
        for row in review["model_decision"].get("missing_relation_concepts") or []:
            name = str(row.get("canonical_name") or "").strip()
            if not name:
                continue
            if normalize_text(name) in {normalize_text(value) for value in _GENERIC_RELATION_NAMES}:
                name = f"{candidate['canonical_name']}{name}"
            evidence_ids = list(row.get("evidence_ids") or [])
            concept_id = stable_id("concept", normalize_text(name))
            result.append({
                "candidate_id": concept_id, "concept_id": concept_id,
                "canonical_name": name, "aliases": list(row.get("aliases") or []),
                "definition": row.get("definition"), "definition_type": "induced",
                "suggested_type": row.get("type"), "evidence_ids": evidence_ids,
                "source_package_ids": list(candidate.get("source_package_ids") or []),
                "source_seed_ids": [candidate["concept_id"]],
                "origin_depth": int(candidate.get("origin_depth") or 0) + 1,
                "occurrence_count": 0, "rank_confidence": "LOW",
                "lifecycle_state": LifecycleState.PENDING_CONCEPT,
                "candidate_origin": "MISSING_RELATION_CONCEPT",
                "evidence": [
                    evidence for evidence in candidate.get("evidence") or []
                    if evidence["evidence_id"] in set(evidence_ids)
                ],
            })
        return result

    def _load_cached(self, package: dict[str, Any]) -> dict[str, Any] | None:
        path = self._result_path(package)
        if not path.is_file():
            return None
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
            if result.get("status") != "success" or result.get("input_fingerprint") != self._fingerprint(package):
                return None
            self._validate(package, result["output"])
            return result
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None

    def _store(self, package: dict[str, Any], result: dict[str, Any]) -> None:
        self._atomic_json(self.output_dir / "packages" / f"{package['package_id']}.json", package)
        self._atomic_json(self._result_path(package), result)

    def _result_path(self, package: dict[str, Any]) -> Path:
        return self.output_dir / "results" / f"{package['package_id']}.json"

    @staticmethod
    def _atomic_json(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def _fingerprint(self, package: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps({
            "package": package, "prompt": self.prompt,
            "model": self.models[self.model_key],
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


__all__ = [
    "SerialConceptAdmissionRunner",
    "build_initial_pending_concepts",
    "build_pending_concepts_from_proposals",
    "recall_concept_evidence",
]
