from __future__ import annotations

from .cleaning import ordered_units
from .models import ContextPackage, DocumentGraph, PipelineRuntime


def build_context_packages(graph: DocumentGraph, runtime: PipelineRuntime) -> list[ContextPackage]:
    order = ordered_units(graph)
    positions = {unit_id: index for index, unit_id in enumerate(order)}
    packages: list[ContextPackage] = []
    for core in graph.units.values():
        if "rule" not in core.role:
            continue
        core_ids = [core.id]
        continuation = [r.target_id for r in graph.relations if r.source_id == core.id and r.type == "CONTINUES"]
        core_ids.extend(continuation)
        support = _support_units(graph, core.id, order, positions)
        aligned_relations = [r for r in graph.relations if r.source_id == core.id and r.type == "ALIGNS_WITH"]
        aligned = [relation.target_id for relation in aligned_relations]
        mentions = [r.target_id for r in graph.relations if r.source_id == core.id and r.type == "MENTIONS"]
        asset_parts = aligned or mentions
        constraints = [
            constraint.id for constraint in graph.constraints.values()
            if constraint.status == "certain" and _covers(constraint, core.id, positions)
        ]
        unresolved = [
            ambiguity.id for ambiguity in graph.ambiguities.values()
            if core.id in ambiguity.source_unit_ids or any(asset in ambiguity.source_unit_ids for asset in asset_parts)
        ]
        if aligned_relations:
            # One passage can explain several table rows. Split by row so no package leaks
            # a row-specific condition into its sibling package.
            for relation in aligned_relations:
                values = set(relation.attributes.get("matched_values", []))
                row_constraints = [
                    constraint.id for constraint in graph.constraints.values()
                    if constraint.status == "certain"
                    and _row_constraint_applies(graph, constraint, core.id, relation.target_id, values, positions)
                ]
                packages.append(ContextPackage(
                    id=f"context_{len(packages) + 1:06d}", core_unit_ids=core_ids,
                    support_unit_ids=sorted(set(support)), constraint_ids=row_constraints,
                    asset_part_ids=[relation.target_id], unresolved_ids=unresolved,
                    attributes={"section_path": core.attributes.get("section_path", []), "routing_evidence": core.attributes.get("route_candidates", []), "split_by_asset_part": True, "matched_values": sorted(values)},
                ))
        else:
            packages.append(ContextPackage(
                id=f"context_{len(packages) + 1:06d}", core_unit_ids=core_ids,
                support_unit_ids=sorted(set(support)), constraint_ids=constraints,
                asset_part_ids=asset_parts, unresolved_ids=unresolved,
                attributes={"section_path": core.attributes.get("section_path", []), "routing_evidence": core.attributes.get("route_candidates", [])},
            ))
    runtime.record(stage="build_context_packages", packages=len(packages))
    return packages


def _support_units(graph: DocumentGraph, unit_id: str, order: list[str], positions: dict[str, int]) -> list[str]:
    support: list[str] = []
    section = [r.target_id for r in graph.relations if r.source_id == unit_id and r.type == "IN_SECTION"]
    support.extend(section)
    for relation in graph.relations:
        if relation.source_id == unit_id and relation.type == "MENTIONS":
            support.append(relation.target_id)
    position = positions[unit_id]
    if position > 0 and graph.units[order[position - 1]].type == "caption":
        support.append(order[position - 1])
    return support


def _covers(constraint, target_id: str, positions: dict[str, int]) -> bool:
    scope = constraint.scope
    if scope.get("scope_type") == "asset_parts":
        return target_id in scope.get("target_unit_ids", [])
    start, end = scope.get("start_unit_id"), scope.get("end_unit_id")
    return start in positions and end in positions and positions[start] <= positions[target_id] <= positions[end]


def _constraint_matches_values(constraint, values: set[str]) -> bool:
    import re
    if not values:
        return False
    numbers = {format(float(value), "g") for value in re.findall(r"(?<!\d)\d+(?:\.\d+)?(?!\d)", str(constraint.value))}
    return bool(numbers & values)


def _row_constraint_applies(graph: DocumentGraph, constraint, core_id: str, asset_part_id: str, values: set[str], positions: dict[str, int]) -> bool:
    """Keep direct row conditions separate from broad prose conditions.

    A chapter-level textual condition can be inherited, but a numeric condition from
    an unrelated paragraph must never be pulled in merely because it shares a value.
    """
    source = graph.units[constraint.source_id]
    if source.type == "table_row":
        return asset_part_id in constraint.scope.get("target_unit_ids", [])
    if constraint.source_id == core_id:
        return _constraint_matches_values(constraint, values)
    return source.type == "title" and not _numbers_in_constraint(constraint) and _covers(constraint, core_id, positions)


def _numbers_in_constraint(constraint) -> set[str]:
    import re
    return {format(float(value), "g") for value in re.findall(r"(?<!\d)\d+(?:\.\d+)?(?!\d)", str(constraint.value))}
