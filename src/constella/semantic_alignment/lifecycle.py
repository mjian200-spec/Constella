from __future__ import annotations

from collections import Counter, defaultdict
from math import ceil
from typing import Any, Iterable

from .models import AlignmentStatus
from .registry import ConceptRegistry, MemorySnapshot, normalize_text


class LifecycleState:
    """Business states used by the concept/object promotion loop."""

    UNPROCESSED_OBJECT = "UNPROCESSED_OBJECT"
    PENDING_CONCEPT = "PENDING_CONCEPT"
    REGISTERED_CONCEPT = "REGISTERED_CONCEPT"


class RankConfidence:
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


RANK_WEIGHTS = (1, 5, 25)
LONG_TAIL_MAX_OCCURRENCE = 5


def rank_by_occurrence(
    rows: Iterable[dict[str, Any]],
    *,
    count_field: str = "occurrence_count",
    identity_field: str = "candidate_id",
) -> list[dict[str, Any]]:
    """Rank all rows by occurrence and split them into 1:5:25 priority bands.

    The ratio applies to positions in the complete descending ranking, not to
    absolute occurrence thresholds. Equal counts use the stable identity as a
    deterministic tie-breaker so every run produces the same bounded bands.
    """

    ranked = [dict(row) for row in rows]
    ranked.sort(key=lambda row: (
        -int(row.get(count_field) or 0),
        normalize_text(str(row.get(identity_field) or "")),
        str(row.get(identity_field) or ""),
    ))
    total = len(ranked)
    if not total:
        return ranked

    weight_total = sum(RANK_WEIGHTS)
    high_end = ceil(total * RANK_WEIGHTS[0] / weight_total)
    medium_end = ceil(total * sum(RANK_WEIGHTS[:2]) / weight_total)
    for index, row in enumerate(ranked, start=1):
        if index <= high_end:
            confidence = RankConfidence.HIGH
        elif index <= medium_end:
            confidence = RankConfidence.MEDIUM
        else:
            confidence = RankConfidence.LOW
        row.update({
            "occurrence_rank": index,
            "rank_confidence": confidence,
            "rank_fraction": round(index / total, 6),
            "rank_population": total,
            "lifecycle_state": row.get("lifecycle_state") or LifecycleState.PENDING_CONCEPT,
        })
    return ranked


def collect_unprocessed_objects(
    object_rows: Iterable[dict[str, Any]],
    registry: ConceptRegistry,
) -> list[dict[str, Any]]:
    """Collect rule objects that cannot resolve to one registered object concept."""

    unresolved: list[dict[str, Any]] = []
    for source in object_rows:
        name = str(source.get("name") or source.get("raw_object") or "").strip()
        if not name:
            continue
        resolution = registry.resolve_exact(name, concept_type="object")
        if resolution["status"] == AlignmentStatus.MATCHED:
            continue
        unresolved.append({
            **source,
            "candidate_id": str(source.get("object_id") or normalize_text(name)),
            "name": name,
            "occurrence_count": int(source.get("frequency") or source.get("occurrence_count") or 1),
            "resolution_status": str(resolution["status"]),
            "lifecycle_state": LifecycleState.UNPROCESSED_OBJECT,
        })
    return rank_by_occurrence(unresolved, identity_field="candidate_id")


def audit_concept_library(memory: MemorySnapshot) -> dict[str, Any]:
    """Return deterministic uniqueness and hierarchy checks for one memory version."""

    registered = [
        row for row in memory.concepts
        if row.get("registration_status") == "APPROVED"
    ]
    owners: dict[str, set[str]] = defaultdict(set)
    for row in registered:
        concept_id = str(row["concept_id"])
        for value in [row.get("canonical_name"), *(row.get("aliases") or [])]:
            key = normalize_text(str(value or ""))
            if key:
                owners[key].add(concept_id)
    collisions = [
        {"normalized_term": term, "concept_ids": sorted(concept_ids)}
        for term, concept_ids in sorted(owners.items())
        if len(concept_ids) > 1
    ]

    registered_ids = {str(row["concept_id"]) for row in registered}
    catalog_ids = {str(row["concept_id"]) for row in memory.concepts}
    missing_endpoints: list[dict[str, Any]] = []
    deferred_candidate_relations = 0
    hierarchy: dict[str, set[str]] = defaultdict(set)
    related_ids: set[str] = set()
    relation_counts: Counter[str] = Counter()
    for relation in memory.relations:
        child = str(relation.get("child_concept_id") or "")
        parent = str(relation.get("parent_concept_id") or "")
        relation_type = str(relation.get("type") or "")
        if relation.get("registration_status") != "APPROVED":
            deferred_candidate_relations += 1
            continue
        if child not in catalog_ids or parent not in catalog_ids:
            missing_endpoints.append({
                "relation_id": relation.get("relation_id"),
                "child_concept_id": child,
                "parent_concept_id": parent,
                "type": relation_type,
            })
            continue
        if child not in registered_ids or parent not in registered_ids:
            missing_endpoints.append({
                "relation_id": relation.get("relation_id"),
                "child_concept_id": child,
                "parent_concept_id": parent,
                "type": relation_type,
            })
            continue
        relation_counts[relation_type] += 1
        related_ids.update((child, parent))
        if relation_type == "IS_A":
            hierarchy[child].add(parent)

    hierarchy_cycles = _hierarchy_cycles(hierarchy)
    return {
        "memory_version": memory.version,
        "registered_concept_count": len(registered),
        "candidate_concept_count": len(memory.concepts) - len(registered),
        "unique_term_count": len(owners),
        "duplicate_term_collision_count": len(collisions),
        "duplicate_term_collisions": collisions,
        "relation_counts": dict(sorted(relation_counts.items())),
        "missing_registered_relation_endpoint_count": len(missing_endpoints),
        "missing_registered_relation_endpoints": missing_endpoints[:20],
        "deferred_candidate_relation_count": deferred_candidate_relations,
        "hierarchy_cycle_count": len(hierarchy_cycles),
        "hierarchy_cycles": hierarchy_cycles,
        "registered_concepts_with_relations": len(related_ids),
        "isolated_registered_concept_count": len(registered_ids - related_ids),
        "invariants": {
            "registered_names_and_aliases_are_unique": not collisions,
            "registered_relation_endpoints_exist": not missing_endpoints,
            "is_a_hierarchy_is_acyclic": not hierarchy_cycles,
        },
    }


def require_complete_alignment(report: dict[str, Any]) -> None:
    runner = report.get("runner") or {}
    failed_count = int(runner.get("failed_count") or 0)
    selected_count = int(runner.get("selected_package_count") or 0)
    success_count = int(runner.get("success_count") or 0)
    if failed_count or success_count != selected_count:
        raise RuntimeError(
            "semantic alignment is incomplete: "
            f"{success_count}/{selected_count} packages succeeded, {failed_count} failed; "
            "resume the lifecycle to retry failed packages"
        )


def _hierarchy_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    cycles: set[tuple[str, ...]] = set()
    visited: set[str] = set()
    active: list[str] = []
    active_positions: dict[str, int] = {}

    def visit(node: str) -> None:
        if node in active_positions:
            cycle = active[active_positions[node]:] + [node]
            body = cycle[:-1]
            if body:
                start = min(range(len(body)), key=lambda index: body[index])
                normalized = tuple(body[start:] + body[:start])
                cycles.add(normalized)
            return
        if node in visited:
            return
        active_positions[node] = len(active)
        active.append(node)
        for parent in sorted(graph.get(node, ())):
            visit(parent)
        active.pop()
        active_positions.pop(node, None)
        visited.add(node)

    for concept_id in sorted(graph):
        visit(concept_id)
    return [list(cycle) + [cycle[0]] for cycle in sorted(cycles)]


__all__ = [
    "LifecycleState",
    "RANK_WEIGHTS",
    "RankConfidence",
    "audit_concept_library",
    "collect_unprocessed_objects",
    "rank_by_occurrence",
    "require_complete_alignment",
]
