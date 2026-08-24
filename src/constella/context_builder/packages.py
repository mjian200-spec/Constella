from __future__ import annotations

from .cleaning import ordered_units
from .models import ContextPackage, DocumentGraph, PipelineRuntime


def build_context_packages(graph: DocumentGraph, runtime: PipelineRuntime) -> list[ContextPackage]:
    order = ordered_units(graph)
    positions = {unit_id: index for index, unit_id in enumerate(order)}
    packages: list[ContextPackage] = []
    packaged_formulas: set[str] = set()
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
        formulas = _mentioned_formulas(graph, core_ids)
        packaged_formulas.update(formulas)
        packages.append(ContextPackage(
            id=f"context_{len(packages) + 1:06d}", core_unit_ids=core_ids,
            support_unit_ids=_support_units(graph, core.id, formulas), constraint_ids=constraints,
            asset_part_ids=_dedupe(mentions + formulas), unresolved_ids=unresolved,
            attributes={"package_type": "rule", "section_path": core.attributes.get("section_path", []), "routing_evidence": core.attributes.get("route_candidates", [])},
        ))
    # Formula references frequently express a calculation or physical
    # relationship without matching the narrow deterministic rule patterns.
    # Give each such formula one reviewable package, containing all direct
    # textual anchors and its contiguous source-text symbol explanations.
    for formula_id in _formula_context_candidates(graph, order):
        if formula_id in packaged_formulas:
            continue
        anchors = _formula_anchors(graph, formula_id)
        core_id = anchors[0]
        constraints = [
            item.id for item in graph.constraints.values()
            if item.status == "certain" and _include_package_constraint(graph, item, core_id, positions)
        ]
        packages.append(ContextPackage(
            id=f"context_{len(packages) + 1:06d}", core_unit_ids=anchors,
            support_unit_ids=_formula_explanations(graph, formula_id), constraint_ids=constraints,
            asset_part_ids=[formula_id], unresolved_ids=[],
            attributes={
                "package_type": "formula_context", "formula_id": formula_id,
                "section_path": graph.units[core_id].attributes.get("section_path", []),
                "routing_evidence": ["explicit_formula_reference"],
            },
        ))
    runtime.record(stage="build_context_packages", packages=len(packages))
    return packages


def _support_units(graph: DocumentGraph, unit_id: str, formulas: list[str]) -> list[str]:
    support = {
        relation.target_id for relation in graph.relations
        if relation.source_id == unit_id and relation.type == "IN_SECTION"
    }
    for formula_id in formulas:
        support.update(_formula_explanations(graph, formula_id))
    return sorted(support)


def _mentioned_formulas(graph: DocumentGraph, source_ids: list[str]) -> list[str]:
    return _dedupe([
        relation.target_id for relation in graph.relations
        if relation.source_id in source_ids and relation.type in {"MENTIONS", "ALIGNS_WITH", "INTRODUCES"}
        and graph.units.get(relation.target_id) is not None and graph.units[relation.target_id].type == "formula"
    ])


def _formula_explanations(graph: DocumentGraph, formula_id: str) -> list[str]:
    return sorted({
        relation.target_id for relation in graph.relations
        if relation.source_id == formula_id and relation.type == "EXPLAINED_BY"
    })


def _formula_anchors(graph: DocumentGraph, formula_id: str) -> list[str]:
    return sorted({
        relation.source_id for relation in graph.relations
        if relation.target_id == formula_id and relation.type in {"MENTIONS", "INTRODUCES"}
        and graph.units.get(relation.source_id) is not None
        and graph.units[relation.source_id].type == "passage"
    })


def _formula_context_candidates(graph: DocumentGraph, order: list[str]) -> list[str]:
    position = {unit_id: index for index, unit_id in enumerate(order)}
    formulas = [
        unit.id for unit in graph.units.values()
        if unit.type == "formula" and _formula_anchors(graph, unit.id)
    ]
    return sorted(formulas, key=lambda unit_id: position[unit_id])


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


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
