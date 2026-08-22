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
        core_ids = [core.id] + [relation.target_id for relation in graph.relations if relation.source_id == core.id and relation.type == "CONTINUES"]
        mentions = [relation.target_id for relation in graph.relations if relation.source_id == core.id and relation.type == "MENTIONS"]
        constraints = [
            item.id for item in graph.constraints.values()
            if item.status == "certain" and _include_package_constraint(graph, item, core.id, positions)
        ]
        unresolved = [item.id for item in graph.ambiguities.values() if core.id in item.source_unit_ids]
        packages.append(ContextPackage(
            id=f"context_{len(packages) + 1:06d}", core_unit_ids=core_ids,
            support_unit_ids=_support_units(graph, core.id), constraint_ids=constraints,
            asset_part_ids=mentions, unresolved_ids=unresolved,
            attributes={"section_path": core.attributes.get("section_path", []), "routing_evidence": core.attributes.get("route_candidates", [])},
        ))
    runtime.record(stage="build_context_packages", packages=len(packages))
    return packages


def _support_units(graph: DocumentGraph, unit_id: str) -> list[str]:
    return sorted({relation.target_id for relation in graph.relations if relation.source_id == unit_id and relation.type in {"IN_SECTION", "MENTIONS"}})


def _covers(constraint, target_id: str, positions: dict[str, int]) -> bool:
    scope = constraint.scope
    start, end = scope.get("start_unit_id"), scope.get("end_unit_id")
    return start in positions and end in positions and positions[start] <= positions[target_id] <= positions[end]


def _include_package_constraint(graph: DocumentGraph, constraint, core_id: str, positions: dict[str, int]) -> bool:
    """Keep inherited text conditions and containing-heading conditions, never the rule's own."""
    source = graph.units.get(constraint.source_id)
    if source is None or constraint.source_id not in positions:
        return False
    if source.type == "title":
        return str(source.content) in graph.units[core_id].attributes.get("section_path", [])
    return positions[constraint.source_id] < positions[core_id] and _covers(constraint, core_id, positions)
