from __future__ import annotations

from collections import Counter, defaultdict
import json
from pathlib import Path
from typing import Any, Iterable

from .models import (
    AlignmentStatus,
    ConceptType,
    MatchMethod,
    ProposalKind,
    SCHEMA_VERSION,
    SemanticRole,
    StructureStatus,
    combine_alignment_statuses,
)
from .packages import SemanticPackageBuilder
from .registry import ConceptRegistry, normalize_text, stable_id
from .state_normalizer import StateNormalizer


def assemble_semantics(
    builder: SemanticPackageBuilder,
    packages: list[dict[str, Any]],
    results: list[dict[str, Any]],
    *,
    selected_object_ids: set[str],
    proposal_threshold: int = 5,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
]:
    registry = builder.registry
    # Collect proposal candidates first; apply the support threshold only after
    # cross-record aggregation, so several rare equivalent expressions can
    # jointly unlock a reviewed memory item.
    normalizer = StateNormalizer(registry, proposal_threshold=1)
    package_cases = {
        str(case["object_id"]): {**case, "tier": package["tier"]}
        for package in packages
        for case in package["cases"]
    }
    interpretations = {
        object_id: dict(value)
        for object_id, value in builder.mechanical_interpretations.items()
        if object_id in selected_object_ids
    }
    for result in results:
        if result.get("status") != "success":
            continue
        for row in result["output"].get("interpretations", []):
            interpretations[str(row["object_id"])] = {
                **row,
                "interpretation_method": "llm",
            }
    for object_id in selected_object_ids:
        if object_id not in interpretations:
            interpretations[object_id] = _fallback_interpretation(
                builder.object_rows[object_id], registry,
            )

    proposals = _ProposalAccumulator()
    resolved_templates: dict[str, dict[str, Any]] = {}
    for object_id in selected_object_ids:
        source = builder.object_rows[object_id]
        interpretation = interpretations[object_id]
        resolved_core: list[dict[str, Any]] = []
        for core in interpretation.get("core_objects") or []:
            resolved = _resolve_object_component(
                registry,
                str(core["text"]),
                core.get("concept_id"),
                requested_match_method=core.get("match_method"),
                frequency=int(source["frequency"]),
            )
            resolved_core.append(resolved)
        structure = {
            "ATOMIC": StructureStatus.ATOMIC,
            "DECOMPOSED": StructureStatus.COMPOSED,
            "EXPRESSION_ONLY": StructureStatus.UNRESOLVED,
        }[interpretation["decision"]]
        statuses = [str(row["alignment_status"]) for row in resolved_core]
        if not statuses and int(source["frequency"]) >= proposal_threshold:
            status = AlignmentStatus.PROPOSED
        else:
            status = combine_alignment_statuses(statuses)
        resolved_templates[object_id] = {
            "interpretation_id": stable_id("object_interpretation", {
                "object_id": object_id,
                "memory_version": builder.memory.version,
                "interpretation": interpretation,
            }),
            "structure": structure,
            "alignment_status": status,
            "core_objects": resolved_core,
            "embedded_states": list(interpretation.get("embedded_states") or []),
            "qualifiers": list(interpretation.get("qualifiers") or []),
            "interpretation_method": interpretation.get("interpretation_method", "fallback"),
            "tier": package_cases.get(object_id, {}).get(
                "tier", interpretation.get("tier", "H0"),
            ),
        }

    object_rows: list[dict[str, Any]] = []
    state_rows: list[dict[str, Any]] = []
    selected_states = [
        row for row in builder.state_rows.values()
        if row["object_id"] in selected_object_ids
    ]
    for source in sorted(selected_states, key=lambda row: row["source_state_id"]):
        template = resolved_templates[source["object_id"]]
        object_record_id = stable_id("object_semantic", source["source_state_id"])
        subject_refs = [
            {
                "concept_id": core.get("concept_id"),
                "alignment_status": core["alignment_status"],
            }
            for core in template["core_objects"] if core.get("concept_id")
        ]
        output_core_objects: list[dict[str, Any]] = []
        for core in template["core_objects"]:
            output_core = {key: value for key, value in core.items() if key != "proposal"}
            if core.get("proposal"):
                output_core["proposal_id"] = proposals.add(
                    core["proposal"], frequency=int(source["frequency"]),
                    raw_expression=source["raw_object"], source=source,
                    subject_object_concept_ids=[],
                )
            else:
                output_core["proposal_id"] = None
            output_core_objects.append(output_core)
        intrinsic_ids: list[str] = []
        condition_ids: list[str] = []
        for position, embedded in enumerate(template["embedded_states"]):
            state_record = _embedded_state_record(
                registry,
                normalizer,
                source,
                embedded,
                position=position,
                core_objects=template["core_objects"],
                memory_version=builder.memory.version,
                proposals=proposals,
            )
            state_rows.append(state_record)
            if state_record["semantic_role"] == SemanticRole.OBJECT_INTRINSIC_STATE:
                intrinsic_ids.append(state_record["record_id"])
            else:
                condition_ids.append(state_record["record_id"])
        object_status = str(template["alignment_status"])
        if not template["core_objects"] and object_status == AlignmentStatus.PROPOSED:
            proposal = {
                "proposal_kind": ProposalKind.NORMALIZATION_PATTERN,
                "concept_type": ConceptType.OBJECT,
                "canonical_name": source["raw_object"],
            }
            proposal_id = proposals.add(
                proposal, frequency=int(source["frequency"]),
                raw_expression=source["raw_object"], source=source,
                subject_object_concept_ids=[],
            )
        else:
            proposal_id = None
        object_rows.append({
            "schema_version": SCHEMA_VERSION,
            "record_id": object_record_id,
            "source_state_id": source["source_state_id"],
            "interpretation_id": template["interpretation_id"],
            "raw_object": source["raw_object"],
            "structure": template["structure"],
            "alignment_status": object_status,
            "core_objects": output_core_objects,
            "intrinsic_state_record_ids": intrinsic_ids,
            "condition_record_ids": condition_ids,
            "qualifiers": [{"text": value} for value in template["qualifiers"]],
            "frequency": int(source["frequency"]),
            "source_roles": source["roles"],
            "source_rule_ids": source["rule_ids"],
            "context_package_ids": source["context_package_ids"],
            "memory_version": builder.memory.version,
            "package_tier": template["tier"],
            "interpretation_method": template["interpretation_method"],
            "proposal_id": proposal_id,
        })
        normalized = normalizer.normalize(
            source["raw_state"],
            frequency=int(source["frequency"]),
            raw_object=source["raw_object"],
            subject_object_concept_ids=[
                str(ref["concept_id"]) for ref in subject_refs if ref.get("concept_id")
            ],
        )
        proposal_id = None
        if normalized.get("proposal"):
            proposal_id = proposals.add(
                normalized["proposal"], frequency=int(source["frequency"]),
                raw_expression=source["raw_state"], source=source,
                subject_object_concept_ids=[
                    str(ref["concept_id"]) for ref in subject_refs if ref.get("concept_id")
                ],
            )
        state_rows.append({
            "schema_version": SCHEMA_VERSION,
            "record_id": stable_id("state_semantic", {
                "source_state_id": source["source_state_id"], "role": SemanticRole.RULE_VALUE,
            }),
            "source_state_id": source["source_state_id"],
            "semantic_role": SemanticRole.RULE_VALUE,
            "raw_object": source["raw_object"],
            "raw_state": source["raw_state"],
            "normalized_input_state": source["normalized_state"],
            "canonical_surface": normalized["canonical_surface"],
            "subject_object_refs": subject_refs,
            "state_concept_id": normalized["state_concept_id"],
            "state_candidates": normalized["state_candidates"],
            "match_method": normalized["match_method"],
            "operator_family": normalized["operator_family"],
            "quantity": normalized["quantity"],
            "qualifiers": normalized["qualifiers"],
            "alignment_status": normalized["alignment_status"],
            "frequency": int(source["frequency"]),
            "source_roles": source["roles"],
            "source_rule_ids": source["rule_ids"],
            "context_package_ids": source["context_package_ids"],
            "memory_version": builder.memory.version,
            "proposal_id": proposal_id,
        })

    proposal_rows = _prioritize_proposals(
        proposals.rows(min_support=proposal_threshold), builder,
    )
    allowed_proposal_ids = {row["proposal_id"] for row in proposal_rows}
    for row in object_rows:
        for core in row["core_objects"]:
            if core.get("proposal_id") and core["proposal_id"] not in allowed_proposal_ids:
                core["proposal_id"] = None
                if core["alignment_status"] == AlignmentStatus.PROPOSED:
                    core["alignment_status"] = AlignmentStatus.EXPRESSION_ONLY
        if row.get("proposal_id") and row["proposal_id"] not in allowed_proposal_ids:
            row["proposal_id"] = None
        if row["core_objects"]:
            row["alignment_status"] = combine_alignment_statuses([
                str(core["alignment_status"]) for core in row["core_objects"]
            ])
        elif not row.get("proposal_id"):
            row["alignment_status"] = AlignmentStatus.EXPRESSION_ONLY
    for row in state_rows:
        if row.get("proposal_id") and row["proposal_id"] not in allowed_proposal_ids:
            row["proposal_id"] = None
            if row["alignment_status"] == AlignmentStatus.PROPOSED:
                row["alignment_status"] = AlignmentStatus.EXPRESSION_ONLY
    coverage_rows = _state_coverage(state_rows, registry)
    report = _assembly_report(
        builder, selected_states, object_rows, state_rows, proposal_rows, coverage_rows, registry,
    )
    failed = [key for key, value in report["invariants"].items() if value is not True]
    if failed:
        raise ValueError(f"semantic assembly invariant failed: {', '.join(failed)}")
    return object_rows, state_rows, proposal_rows, coverage_rows, report


def _fallback_interpretation(source: dict[str, Any], registry: ConceptRegistry) -> dict[str, Any]:
    exact = registry.resolve_exact(source["name"], concept_type=ConceptType.OBJECT)
    if exact["status"] in {AlignmentStatus.MATCHED, AlignmentStatus.TYPE_REVIEW}:
        return {
            "object_id": source["object_id"],
            "decision": "ATOMIC",
            "core_objects": [{
                "text": source["name"],
                "concept_id": exact["concept_id"],
                "match_method": exact["match_method"],
            }],
            "embedded_states": [],
            "qualifiers": [],
            "interpretation_method": "failed_package_exact_fallback",
        }
    return {
        "object_id": source["object_id"],
        "decision": "EXPRESSION_ONLY",
        "core_objects": [],
        "embedded_states": [],
        "qualifiers": [],
        "interpretation_method": "failed_package_expression_fallback",
    }


def _resolve_object_component(
    registry: ConceptRegistry,
    text: str,
    candidate_id: str | None,
    *,
    requested_match_method: str | None = None,
    frequency: int,
) -> dict[str, Any]:
    proposal = None
    if candidate_id:
        concept = registry.concepts[str(candidate_id)]
        if concept.get("type") == ConceptType.OBJECT:
            status = AlignmentStatus.MATCHED
        elif not concept.get("type"):
            status = AlignmentStatus.TYPE_REVIEW
            proposal = {
                "proposal_kind": ProposalKind.TYPE_REVIEW,
                "concept_type": ConceptType.OBJECT,
                "concept_id": candidate_id,
                "canonical_name": str(concept.get("canonical_name") or text),
            }
        else:
            status = AlignmentStatus.AMBIGUOUS
        return {
            "text": text,
            "concept_id": candidate_id,
            "alignment_status": status,
            "match_method": requested_match_method or MatchMethod.LLM_CANDIDATE,
            "candidates": [registry.payload(str(candidate_id))],
            "proposal": proposal,
        }
    exact = registry.resolve_exact(text, concept_type=ConceptType.OBJECT)
    status = exact["status"]
    if status == AlignmentStatus.TYPE_REVIEW:
        concept_id = exact["concept_id"]
        proposal = {
            "proposal_kind": ProposalKind.TYPE_REVIEW,
            "concept_type": ConceptType.OBJECT,
            "concept_id": concept_id,
            "canonical_name": str(registry.concepts[str(concept_id)].get("canonical_name") or text),
        }
    elif status == AlignmentStatus.EXPRESSION_ONLY and frequency > 0:
        status = AlignmentStatus.PROPOSED
        proposal = {
            "proposal_kind": ProposalKind.OBJECT_CONCEPT,
            "concept_type": ConceptType.OBJECT,
            "canonical_name": text,
        }
    return {
        "text": text,
        "concept_id": exact.get("concept_id"),
        "alignment_status": status,
        "match_method": exact.get("match_method", MatchMethod.NONE),
        "candidates": exact.get("candidates", []),
        "proposal": proposal,
    }


def _embedded_state_record(
    registry: ConceptRegistry,
    normalizer: StateNormalizer,
    source: dict[str, Any],
    embedded: dict[str, Any],
    *,
    position: int,
    core_objects: list[dict[str, Any]],
    memory_version: str,
    proposals: _ProposalAccumulator,
) -> dict[str, Any]:
    subject_text = str(embedded["subject_text"])
    matching_core = [
        core for core in core_objects
        if normalize_text(core["text"]) == normalize_text(subject_text)
    ]
    if matching_core:
        subject_refs = [
            {"concept_id": core.get("concept_id"), "alignment_status": core["alignment_status"]}
            for core in matching_core if core.get("concept_id")
        ]
    else:
        resolved = _resolve_object_component(
            registry, subject_text, None, frequency=int(source["frequency"]),
        )
        subject_refs = [{
            "concept_id": resolved.get("concept_id"),
            "alignment_status": resolved["alignment_status"],
        }] if resolved.get("concept_id") else []
        if resolved.get("proposal"):
            proposals.add(
                resolved["proposal"], frequency=int(source["frequency"]),
                raw_expression=subject_text, source=source, subject_object_concept_ids=[],
            )
    normalized = normalizer.normalize(
        str(embedded["state_text"]),
        frequency=int(source["frequency"]),
        raw_object=subject_text,
        subject_object_concept_ids=[str(ref["concept_id"]) for ref in subject_refs],
    )
    proposal_id = None
    if normalized.get("proposal"):
        proposal_id = proposals.add(
            normalized["proposal"], frequency=int(source["frequency"]),
            raw_expression=str(embedded["state_text"]), source=source,
            subject_object_concept_ids=[str(ref["concept_id"]) for ref in subject_refs],
        )
    role = str(embedded["role"])
    return {
        "schema_version": SCHEMA_VERSION,
        "record_id": stable_id("state_semantic", {
            "source_state_id": source["source_state_id"],
            "role": role,
            "position": position,
            "subject": normalize_text(subject_text),
            "state": normalize_text(str(embedded["state_text"])),
        }),
        "source_state_id": source["source_state_id"],
        "semantic_role": role,
        "raw_object": subject_text,
        "raw_state": str(embedded["state_text"]),
        "normalized_input_state": None,
        "canonical_surface": normalized["canonical_surface"],
        "subject_object_refs": subject_refs,
        "state_concept_id": normalized["state_concept_id"],
        "state_candidates": normalized["state_candidates"],
        "match_method": normalized["match_method"],
        "operator_family": normalized["operator_family"],
        "quantity": normalized["quantity"],
        "qualifiers": normalized["qualifiers"],
        "alignment_status": normalized["alignment_status"],
        "frequency": int(source["frequency"]),
        "source_roles": source["roles"],
        "source_rule_ids": source["rule_ids"],
        "context_package_ids": source["context_package_ids"],
        "memory_version": memory_version,
        "proposal_id": proposal_id,
    }


class _ProposalAccumulator:
    def __init__(self) -> None:
        self.values: dict[tuple[Any, ...], dict[str, Any]] = {}

    def add(
        self,
        proposal: dict[str, Any],
        *,
        frequency: int,
        raw_expression: str,
        source: dict[str, Any],
        subject_object_concept_ids: list[str],
    ) -> str:
        kind = str(proposal["proposal_kind"])
        concept_type = str(proposal.get("concept_type") or "")
        name = str(proposal.get("canonical_name") or raw_expression)
        concept_id = str(proposal.get("concept_id") or "")
        subject_ids = tuple(sorted(subject_object_concept_ids))
        dimension_key = "|".join(subject_ids)
        if not dimension_key:
            dimension_key = normalize_text(str(proposal.get("raw_object") or source["raw_object"]))
        key = (kind, concept_type, concept_id, normalize_text(name), dimension_key)
        proposal_id = stable_id("alignment_proposal", key)
        row = self.values.setdefault(key, {
            "schema_version": SCHEMA_VERSION,
            "proposal_id": proposal_id,
            "proposal_kind": kind,
            "concept_type": concept_type or None,
            "concept_id": concept_id or None,
            "canonical_name": name,
            "subject_object_concept_ids": list(subject_ids),
            "subject_dimension_key": dimension_key,
            "support": 0,
            "raw_expressions": set(),
            "source_state_ids": set(),
            "source_rule_ids": set(),
            "context_package_ids": set(),
            "review_status": "PENDING",
        })
        row["support"] += frequency
        row["raw_expressions"].add(raw_expression)
        row["source_state_ids"].add(source["source_state_id"])
        row["source_rule_ids"].update(source["rule_ids"])
        row["context_package_ids"].update(source["context_package_ids"])
        return proposal_id

    def rows(self, *, min_support: int) -> list[dict[str, Any]]:
        result = []
        for row in self.values.values():
            if row["proposal_kind"] not in {ProposalKind.TYPE_REVIEW, ProposalKind.ALIAS} \
                    and int(row["support"]) < min_support:
                continue
            result.append({
                **row,
                "raw_expressions": sorted(row["raw_expressions"]),
                "source_state_ids": sorted(row["source_state_ids"]),
                "source_rule_ids": sorted(row["source_rule_ids"]),
                "context_package_ids": sorted(row["context_package_ids"]),
            })
        return sorted(result, key=lambda row: (-int(row["support"]), row["proposal_id"]))


def _prioritize_proposals(
    rows: list[dict[str, Any]],
    builder: SemanticPackageBuilder,
) -> list[dict[str, Any]]:
    object_expressions = [normalize_text(row["name"]) for row in builder.object_rows.values()]
    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    result = []
    for source in rows:
        row = dict(source)
        name = normalize_text(str(row.get("canonical_name") or ""))
        unlock_count = sum(bool(name and name in expression) for expression in object_expressions)
        kind = str(row["proposal_kind"])
        support = int(row["support"])
        if kind == ProposalKind.TYPE_REVIEW:
            priority = "P0"
        elif unlock_count >= 5 and support >= 20:
            priority = "P0"
        elif unlock_count >= 5 or support >= 20:
            priority = "P1"
        elif kind in {ProposalKind.OBJECT_CONCEPT, ProposalKind.STATE_CONCEPT, ProposalKind.ALIAS}:
            priority = "P2"
        else:
            priority = "P3"
        row["unlock_count"] = unlock_count
        row["review_priority"] = priority
        result.append(row)
    return sorted(result, key=lambda row: (
        order[row["review_priority"]], -int(row["unlock_count"]),
        -int(row["support"]), row["proposal_id"],
    ))


def _state_coverage(state_rows: list[dict[str, Any]], registry: ConceptRegistry) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in state_rows:
        state_id = str(row.get("state_concept_id") or "")
        if row.get("alignment_status") != AlignmentStatus.MATCHED or state_id not in registry:
            continue
        object_ids = [
            str(ref["concept_id"])
            for ref in row.get("subject_object_refs") or []
            if ref.get("alignment_status") == AlignmentStatus.MATCHED and ref.get("concept_id") in registry
        ]
        for object_id in object_ids:
            observation = grouped[object_id].setdefault(state_id, {
                "state_concept_id": state_id,
                "canonical_name": str(registry.concepts[state_id].get("canonical_name") or ""),
                "support": 0,
                "source_state_ids": set(),
                "parameter_observations": Counter(),
            })
            observation["support"] += int(row["frequency"])
            observation["source_state_ids"].add(row["source_state_id"])
            if row.get("quantity"):
                key = json.dumps({
                    "operator_family": row.get("operator_family"),
                    "quantity": row["quantity"],
                }, ensure_ascii=False, sort_keys=True)
                observation["parameter_observations"][key] += int(row["frequency"])
    result = []
    for object_id, states in grouped.items():
        observations = []
        for observation in states.values():
            observations.append({
                **observation,
                "source_state_count": len(observation["source_state_ids"]),
                "source_state_ids": sorted(observation["source_state_ids"]),
                "parameter_observations": [
                    {**json.loads(key), "support": support}
                    for key, support in observation["parameter_observations"].most_common()
                ],
            })
        observations.sort(key=lambda row: (-int(row["support"]), row["state_concept_id"]))
        result.append({
            "schema_version": SCHEMA_VERSION,
            "object_concept_id": object_id,
            "total_support": sum(int(row["support"]) for row in observations),
            "observations": observations,
        })
    return sorted(result, key=lambda row: row["object_concept_id"])


def _assembly_report(
    builder: SemanticPackageBuilder,
    selected_states: list[dict[str, Any]],
    object_rows: list[dict[str, Any]],
    state_rows: list[dict[str, Any]],
    proposal_rows: list[dict[str, Any]],
    coverage_rows: list[dict[str, Any]],
    registry: ConceptRegistry,
) -> dict[str, Any]:
    expected_ids = {row["source_state_id"] for row in selected_states}
    object_ids = [row["source_state_id"] for row in object_rows]
    rule_state_rows = [row for row in state_rows if row["semantic_role"] == SemanticRole.RULE_VALUE]
    rule_state_ids = [row["source_state_id"] for row in rule_state_rows]
    invalid_refs = []
    for row in object_rows:
        invalid_refs.extend(
            core.get("concept_id") for core in row["core_objects"]
            if core.get("concept_id") and core["concept_id"] not in registry
        )
    for row in state_rows:
        if row.get("state_concept_id") and row["state_concept_id"] not in registry:
            invalid_refs.append(row["state_concept_id"])
        invalid_refs.extend(
            ref.get("concept_id") for ref in row.get("subject_object_refs") or []
            if ref.get("concept_id") and ref["concept_id"] not in registry
        )
    source_by_id = {row["source_state_id"]: row for row in selected_states}
    raw_unchanged = all(
        row["raw_object"] == source_by_id[row["source_state_id"]]["raw_object"]
        and row["raw_state"] == source_by_id[row["source_state_id"]]["raw_state"]
        for row in rule_state_rows
    )
    expected_frequency = sum(int(row["frequency"]) for row in selected_states)
    actual_frequency = sum(int(row["frequency"]) for row in rule_state_rows)
    return {
        "schema_version": SCHEMA_VERSION,
        "source_state_count": len(expected_ids),
        "source_occurrence_count": expected_frequency,
        "object_semantic_count": len(object_rows),
        "state_semantic_count": len(state_rows),
        "derived_state_count": len(state_rows) - len(rule_state_rows),
        "object_status_counts": dict(Counter(str(row["alignment_status"]) for row in object_rows)),
        "state_status_counts": dict(Counter(str(row["alignment_status"]) for row in rule_state_rows)),
        "proposal_count": len(proposal_rows),
        "proposal_counts": dict(Counter(str(row["proposal_kind"]) for row in proposal_rows)),
        "coverage_object_count": len(coverage_rows),
        "memory_version": builder.memory.version,
        "registry_concept_count": len(registry.concepts),
        "concepts_created_by_alignment": 0,
        "invariants": {
            "one_object_record_per_source_state": len(object_ids) == len(set(object_ids)) and set(object_ids) == expected_ids,
            "one_rule_value_per_source_state": len(rule_state_ids) == len(set(rule_state_ids)) and set(rule_state_ids) == expected_ids,
            "derived_states_have_source": all(row.get("source_state_id") in expected_ids for row in state_rows),
            "raw_fields_unchanged": raw_unchanged,
            "source_frequency_preserved": actual_frequency == expected_frequency,
            "all_concept_references_in_memory": not invalid_refs,
            "zero_concept_creation": True,
        },
    }


def write_jsonl(path: str | Path, rows: Iterable[dict[str, Any]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    temporary.replace(target)


def write_json(path: str | Path, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
