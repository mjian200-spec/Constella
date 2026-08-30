from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any

import yaml

from constella.context_builder.llm_client import LLMClient

from .models import ConceptType, ProposalKind
from .lifecycle import LifecycleState, rank_by_occurrence
from .registry import MemorySnapshot, stable_id


_DECISIONS = {"APPROVE", "DEFER", "REJECT"}
_CHECKS = (
    "stable_kind",
    "not_instance_or_parameter",
    "single_identity",
    "evidence_sufficient",
    "type_clear",
)
_NUMERIC_EXPRESSION = re.compile(r"^[\d\s.,，;；:：%°℃<>≤≥=+\-~～—–/\\]+$")


def build_concept_admission_candidates(
    proposal_rows: list[dict[str, Any]],
    memory: MemorySnapshot,
    *,
    min_support: int | None = None,
) -> list[dict[str, Any]]:
    """Collapse proposals and rank every eligible concept by occurrence.

    ``min_support`` remains accepted for CLI compatibility but is deliberately
    not used as a threshold. Promotion priority is determined by the complete
    occurrence ranking and the 1:5:25 bands.
    """
    grouped: dict[str, dict[str, Any]] = {}
    concepts = {str(row["concept_id"]): row for row in memory.concepts}
    for proposal in proposal_rows:
        if proposal.get("proposal_kind") not in {
            ProposalKind.TYPE_REVIEW, ProposalKind.CONCEPT_APPROVAL,
        }:
            continue
        concept_id = str(proposal.get("concept_id") or "")
        concept_type = str(proposal.get("concept_type") or "")
        concept = concepts.get(concept_id)
        if not concept or concept.get("registration_status") == "APPROVED":
            continue
        if concept_type not in {ConceptType.OBJECT, ConceptType.STATE}:
            continue
        row = grouped.setdefault(concept_id, {
            "concept_id": concept_id,
            "canonical_name": str(concept.get("canonical_name") or ""),
            "aliases": list(concept.get("aliases") or []),
            "definition": concept.get("definition"),
            "definition_type": concept.get("definition_type"),
            "source_audit_status": concept.get("audit_status"),
            "concept_evidence_ids": list(concept.get("evidence_ids") or []),
            "concept_source_package_ids": list(concept.get("source_package_ids") or []),
            "concept_source_seed_ids": list(concept.get("source_seed_ids") or []),
            "concept_origin_depth": concept.get("origin_depth", 0),
            "suggested_types": set(),
            "support_by_type": defaultdict(int),
            "raw_expressions_by_type": defaultdict(set),
            "dimensions_by_type": defaultdict(set),
            "source_state_ids": set(),
            "source_rule_ids": set(),
            "context_package_ids": set(),
            "unlock_count": 0,
        })
        row["suggested_types"].add(concept_type)
        row["support_by_type"][concept_type] = max(
            row["support_by_type"][concept_type], int(proposal.get("support") or 0),
        )
        row["raw_expressions_by_type"][concept_type].update(proposal.get("raw_expressions") or [])
        if proposal.get("subject_dimension_key"):
            row["dimensions_by_type"][concept_type].add(str(proposal["subject_dimension_key"]))
        row["source_state_ids"].update(proposal.get("source_state_ids") or [])
        row["source_rule_ids"].update(proposal.get("source_rule_ids") or [])
        row["context_package_ids"].update(proposal.get("context_package_ids") or [])
        row["unlock_count"] = max(row["unlock_count"], int(proposal.get("unlock_count") or 0))

    result: list[dict[str, Any]] = []
    for row in grouped.values():
        maximum_support = max(row["support_by_type"].values(), default=0)
        name = row["canonical_name"].strip()
        deterministic_checks = {
            "known_concept": bool(name),
            "has_article_evidence": bool(
                row["concept_evidence_ids"] or row["concept_source_package_ids"]
            ),
            "not_numeric_expression": not bool(_NUMERIC_EXPRESSION.fullmatch(name)),
        }
        if not all(deterministic_checks.values()):
            continue
        result.append({
            **{key: value for key, value in row.items() if key not in {
                "suggested_types", "support_by_type", "raw_expressions_by_type",
                "dimensions_by_type", "source_state_ids", "source_rule_ids",
                "context_package_ids",
            }},
            "suggested_types": sorted(row["suggested_types"]),
            "support_by_type": dict(row["support_by_type"]),
            "raw_expressions_by_type": {
                key: sorted(values)[:12] for key, values in row["raw_expressions_by_type"].items()
            },
            "dimensions_by_type": {
                key: sorted(values)[:12] for key, values in row["dimensions_by_type"].items()
            },
            "source_state_count": len(row["source_state_ids"]),
            "source_rule_count": len(row["source_rule_ids"]),
            "context_package_count": len(row["context_package_ids"]),
            "source_state_ids": sorted(row["source_state_ids"])[:20],
            "context_package_ids": sorted(row["context_package_ids"])[:20],
            "deterministic_checks": deterministic_checks,
            "occurrence_count": maximum_support,
            "candidate_id": row["concept_id"],
            "lifecycle_state": LifecycleState.PENDING_CONCEPT,
        })
    return rank_by_occurrence(result, identity_field="candidate_id")


class ConceptAdmissionGate:
    """Model audit with deterministic post-validation for identity and type."""

    def __init__(
        self,
        models: dict[str, Any],
        model_key: str,
        prompt_path: str | Path,
        output_dir: str | Path,
        *,
        workers: int = 1,
        batch_size: int = 12,
        client=None,
    ) -> None:
        if workers < 1 or batch_size < 1:
            raise ValueError("workers and batch_size must be positive")
        self.models = models
        self.model_key = model_key
        self.output_dir = Path(output_dir)
        self.workers = workers
        self.batch_size = batch_size
        self.client = client or LLMClient(models)
        self.prompt = yaml.safe_load(Path(prompt_path).read_text(encoding="utf-8"))
        if not isinstance(self.prompt, dict) or not {"id", "version", "system"} <= set(self.prompt):
            raise ValueError("invalid concept type gate prompt")

    def run(
        self,
        candidates: list[dict[str, Any]],
        *,
        memory_version: str,
        refresh: bool = False,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        packages = [self._package(candidates[start:start + self.batch_size], memory_version)
                    for start in range(0, len(candidates), self.batch_size)]
        results: list[dict[str, Any] | None] = [None] * len(packages)
        cached_count = 0
        pending: list[tuple[int, dict[str, Any]]] = []
        for index, package in enumerate(packages):
            cached = None if refresh else self._load_cached(package)
            if cached is None:
                pending.append((index, package))
            else:
                results[index] = cached
                cached_count += 1
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(self._process, package): index for index, package in pending}
            for future in as_completed(futures):
                index = futures[future]
                try:
                    results[index] = future.result()
                except Exception as error:
                    results[index] = {
                        "package_id": packages[index]["package_id"], "status": "failed",
                        "errors": [f"unhandled:{type(error).__name__}: {error}"],
                    }
        final = [row for row in results if row is not None]
        reviews: list[dict[str, Any]] = []
        by_id = {str(row["concept_id"]): row for row in candidates}
        for result in final:
            if result.get("status") != "success":
                continue
            for decision in result["output"]["decisions"]:
                candidate = by_id[str(decision["concept_id"])]
                approved, gate_reason = self._approval_gate(candidate, decision)
                reviews.append({
                    "review_id": stable_id("model_type_review", {
                        "memory_version": memory_version,
                        "concept_id": decision["concept_id"],
                        "prompt_version": self.prompt["version"],
                    }),
                    "memory_version": memory_version,
                    "concept_id": decision["concept_id"],
                    "canonical_name": candidate["canonical_name"],
                    "suggested_types": candidate["suggested_types"],
                    "concept_snapshot": {
                        key: candidate.get(key) for key in (
                            "concept_id", "canonical_name", "aliases", "definition",
                            "definition_type", "source_audit_status", "concept_evidence_ids",
                            "concept_source_package_ids",
                        )
                    },
                    "model_decision": decision,
                    "gate_status": "APPROVED" if approved else "NOT_APPROVED",
                    "gate_reason": gate_reason,
                    "prompt_id": self.prompt["id"],
                    "prompt_version": str(self.prompt["version"]),
                    "configured_model": self.models[self.model_key]["model"],
                    "package_id": result["package_id"],
                })
        events = [self._event(row) for row in reviews if row["gate_status"] == "APPROVED"]
        report = {
            "candidate_count": len(candidates),
            "package_count": len(packages),
            "success_count": sum(row.get("status") == "success" for row in final),
            "failed_count": sum(row.get("status") != "success" for row in final),
            "cached_count": cached_count,
            "approved_count": len(events),
            "deferred_count": sum(
                row["model_decision"]["decision"] == "DEFER" for row in reviews
            ),
            "rejected_count": sum(
                row["model_decision"]["decision"] == "REJECT" for row in reviews
            ),
            "post_gate_rejected_count": sum(
                row["model_decision"]["decision"] == "APPROVE"
                and row["gate_status"] != "APPROVED" for row in reviews
            ),
        }
        return reviews, events, report

    def _package(self, rows: list[dict[str, Any]], memory_version: str) -> dict[str, Any]:
        payload = {
            "package_type": "concept_type_gate",
            "memory_version": memory_version,
            "candidates": rows,
        }
        payload["package_id"] = stable_id("concept_type_gate", payload)
        return payload

    def _process(self, package: dict[str, Any]) -> dict[str, Any]:
        messages = [
            {"role": "system", "content": self.prompt["system"]},
            {"role": "user", "content": json.dumps(package, ensure_ascii=False)},
        ]
        errors: list[str] = []
        for attempt in range(1, 3):
            try:
                response = self.client.complete(
                    self.model_key, messages, response_format={"type": "json_object"},
                    prompt_id=self.prompt["id"], prompt_version=str(self.prompt["version"]),
                    input_unit_ids=[], max_tokens=int(self.prompt.get("max_tokens", 2400)),
                )
                value = json.loads(response["choices"][0]["message"]["content"])
                self._validate(package, value)
                result = {
                    "package_id": package["package_id"], "status": "success",
                    "attempt_count": attempt, "input_fingerprint": self._fingerprint(package),
                    "served_model": response.get("model"), "output": value,
                }
                self._store(package, result)
                return result
            except Exception as error:
                errors.append(f"{type(error).__name__}: {error}")
                if attempt == 1:
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
    def _validate(package: dict[str, Any], value: dict[str, Any]) -> None:
        rows = value.get("decisions") if isinstance(value, dict) else None
        if not isinstance(rows, list):
            raise ValueError("decisions must be a list")
        candidates = {str(row["concept_id"]): row for row in package["candidates"]}
        ids = [str(row.get("concept_id") or "") for row in rows if isinstance(row, dict)]
        if len(ids) != len(set(ids)) or set(ids) != set(candidates):
            raise ValueError("decisions must cover every concept exactly once")
        for row in rows:
            if row.get("decision") not in _DECISIONS:
                raise ValueError("invalid decision")
            if row.get("selected_type") not in {None, ConceptType.OBJECT, ConceptType.STATE}:
                raise ValueError("invalid selected_type")
            if row["decision"] == "APPROVE" and row.get("selected_type") is None:
                raise ValueError("APPROVE requires selected_type")
            if row["decision"] != "APPROVE" and row.get("selected_type") is not None:
                raise ValueError("non-APPROVE decision requires null selected_type")
            checks = row.get("boundary_checks")
            if not isinstance(checks, dict) or set(checks) != set(_CHECKS):
                raise ValueError("boundary_checks must contain the exact protocol fields")
            if not all(isinstance(checks[key], bool) for key in _CHECKS):
                raise ValueError("boundary checks must be booleans")
            if row.get("confidence") not in {"HIGH", "MEDIUM", "LOW"}:
                raise ValueError("invalid confidence")
            if not isinstance(row.get("reason"), str) or not row["reason"].strip():
                raise ValueError("reason is required")

    @staticmethod
    def _approval_gate(candidate: dict[str, Any], decision: dict[str, Any]) -> tuple[bool, str]:
        if decision["decision"] != "APPROVE":
            return False, f"model_{str(decision['decision']).lower()}"
        if decision["confidence"] != "HIGH":
            return False, "approval_requires_high_confidence"
        if not all(decision["boundary_checks"].values()):
            return False, "all_boundary_checks_must_pass"
        selected = str(decision["selected_type"])
        if selected not in candidate["suggested_types"]:
            return False, "selected_type_not_observed"
        return True, "model_and_deterministic_gate_passed"

    def _event(self, review: dict[str, Any]) -> dict[str, Any]:
        selected_type = str(review["model_decision"]["selected_type"])
        snapshot = review["concept_snapshot"]
        concept = {
            "concept_id": snapshot["concept_id"],
            "canonical_name": snapshot["canonical_name"],
            "aliases": list(snapshot.get("aliases") or []),
            "definition": snapshot.get("definition"),
            "definition_type": snapshot.get("definition_type"),
            "source_package_ids": list(snapshot.get("concept_source_package_ids") or []),
            "evidence_ids": list(snapshot.get("concept_evidence_ids") or []),
            "audit_status": snapshot.get("source_audit_status"),
            "source_seed_ids": list(snapshot.get("concept_source_seed_ids") or []),
            "origin_depth": int(snapshot.get("concept_origin_depth") or 0),
            "type": selected_type,
            "registration_status": "APPROVED",
        }
        return {
            "event_id": stable_id("memory_event", {
                "review_id": review["review_id"], "type": selected_type,
            }),
            "status": "APPROVED",
            "proposal_kind": ProposalKind.CONCEPT_APPROVAL,
            "concept_id": review["concept_id"],
            "type": selected_type,
            "concept": concept,
            "approval_mode": "MODEL_GATE",
            "reason": review["model_decision"]["reason"],
            "boundary_checks": review["model_decision"]["boundary_checks"],
            "confidence": review["model_decision"]["confidence"],
            "base_memory_version": review["memory_version"],
            "prompt_id": review["prompt_id"],
            "prompt_version": review["prompt_version"],
            "configured_model": review["configured_model"],
            "source_review_id": review["review_id"],
        }

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
        return hashlib.sha256(json.dumps(
            {"package": package, "prompt": self.prompt, "model": self.models[self.model_key]},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()


__all__ = ["ConceptAdmissionGate", "build_concept_admission_candidates"]
