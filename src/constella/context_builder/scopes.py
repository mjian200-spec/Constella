from __future__ import annotations

from .cleaning import ordered_units
from .models import DocumentGraph, PipelineRuntime


CONTEXT_SWITCH_TYPES = {"材料", "焊接方法", "保护气体", "焊丝", "试验"}


def resolve_constraint_scopes(graph: DocumentGraph, runtime: PipelineRuntime) -> None:
    order = ordered_units(graph)
    positions = {unit_id: index for index, unit_id in enumerate(order)}
    constraints = sorted(graph.constraints.values(), key=lambda item: (positions.get(item.source_id, len(order)), item.id))
    for index, constraint in enumerate(constraints):
        start = positions.get(constraint.source_id)
        if start is None:
            constraint.status = "uncertain"
            constraint.scope["scope_type"] = "uncertain"
            continue
        end = _section_end(graph, order, positions, constraint.source_id)
        for successor in constraints[index + 1:]:
            successor_start = positions.get(successor.source_id)
            if successor_start is None or successor_start > end:
                continue
            if successor.type == constraint.type or _is_context_switch(constraint.type, successor.type):
                end = max(start, successor_start - 1)
                break
        constraint.scope = {
            "scope_type": "unit_range", "start_unit_id": constraint.source_id,
            "end_unit_id": order[end], "target_unit_ids": [], "candidate_ranges": [],
        }
    runtime.record(stage="resolve_constraint_scopes", resolved=len(graph.constraints))


def _section_end(graph: DocumentGraph, order: list[str], positions: dict[str, int], source_id: str) -> int:
    source = graph.units[source_id]
    start = positions[source_id]
    section_title_id = next((relation.target_id for relation in graph.relations if relation.source_id == source_id and relation.type == "IN_SECTION"), None)
    level = graph.units[section_title_id].attributes.get("heading_level") if section_title_id else None
    if source.type == "title":
        level = source.attributes.get("heading_level")
    for unit_id in order[start + 1:]:
        candidate = graph.units[unit_id]
        if level is not None and candidate.type == "title" and candidate.attributes.get("heading_level", 99) <= level:
            return positions[unit_id] - 1
    return len(order) - 1


def _is_context_switch(current_type: str, next_type: str) -> bool:
    return current_type in CONTEXT_SWITCH_TYPES and next_type in CONTEXT_SWITCH_TYPES and current_type != next_type
