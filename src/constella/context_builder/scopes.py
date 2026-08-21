from __future__ import annotations

from .cleaning import ordered_units
from .models import DocumentGraph, PipelineRuntime


def resolve_constraint_scopes(graph: DocumentGraph, runtime: PipelineRuntime) -> None:
    order = ordered_units(graph)
    positions = {unit_id: index for index, unit_id in enumerate(order)}
    for constraint in graph.constraints.values():
        source = graph.units[constraint.source_id]
        if source.type in {"table_cell", "table_row"}:
            row_id = source.id if source.type == "table_row" else source.id.rsplit("_cell_", 1)[0]
            constraint.scope = {"scope_type": "asset_parts", "start_unit_id": None, "end_unit_id": None, "target_unit_ids": [row_id], "candidate_ranges": []}
            continue
        start = positions.get(source.id)
        if start is None:
            constraint.status = "uncertain"; continue
        level = source.attributes.get("heading_level") if source.type == "title" else None
        end = len(order) - 1
        if level is not None:
            for candidate_id in order[start + 1:]:
                candidate = graph.units[candidate_id]
                if candidate.type == "title" and candidate.attributes.get("heading_level", 99) <= level:
                    end = positions[candidate_id] - 1; break
        constraint.scope = {"scope_type": "unit_range", "start_unit_id": source.id, "end_unit_id": order[end], "target_unit_ids": [], "candidate_ranges": []}
    runtime.record(stage="resolve_constraint_scopes", resolved=len(graph.constraints))
