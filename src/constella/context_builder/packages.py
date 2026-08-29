from __future__ import annotations

import re

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
        mentions = [
            relation.target_id for relation in graph.relations
            if relation.source_id == core.id and relation.type == "MENTIONS"
            and (
                graph.units.get(relation.target_id) is not None
                and (
                    graph.units[relation.target_id].type == "formula"
                    or _asset_is_useful(graph, relation.target_id)
                )
            )
        ]
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
        anchors = _formula_anchors(graph, formula_id) or _adjacent_text_units(graph, formula_id, order)
        core_ids = anchors or [formula_id]
        core_id = core_ids[0]
        constraints = [
            item.id for item in graph.constraints.values()
            if item.status == "certain" and _include_package_constraint(graph, item, core_id, positions)
        ]
        packages.append(ContextPackage(
            id=f"context_{len(packages) + 1:06d}", core_unit_ids=core_ids,
            support_unit_ids=_dedupe(
                _formula_explanations(graph, formula_id)
                + [item for item in _adjacent_text_units(graph, formula_id, order) if item not in anchors]
            ), constraint_ids=constraints,
            asset_part_ids=[formula_id], unresolved_ids=[],
            attributes={
                "package_type": "formula_context", "formula_id": formula_id,
                "section_path": graph.units[core_id].attributes.get("section_path", []),
                "routing_evidence": ["explicit_formula_reference"],
            },
        ))
    _append_article_candidate_packages(graph, packages, order)
    runtime.record(stage="build_context_packages", packages=len(packages))
    return packages


_CONCEPT_ANCHOR = re.compile(
    r"(?:是指|定义为|称为|又称|简称|是一种|是一类|属于|分为|可分为|包括|包含|由.{0,30}组成|构成)"
)
_STRUCTURAL_HEADING = re.compile(r"(?:的)?(?:分类|种类|类型|组成|结构)$")
_LIST_MARKER = re.compile(r"^\s*(?:\d+[.、）)]|[（(]\d+[）)]|[①②③④⑤⑥⑦⑧⑨⑩]|[一二三四五六七八九十]+、)")


def _append_article_candidate_packages(
    graph: DocumentGraph, packages: list[ContextPackage], order: list[str], *, neighbor_radius: int = 2,
) -> None:
    """Add article-driven candidates without assigning their final rule/concept roles."""
    positions = {unit_id: index for index, unit_id in enumerate(order)}
    covered = {
        unit_id for package in packages
        for unit_id in package.core_unit_ids + package.support_unit_ids + package.asset_part_ids
    }
    claimed: set[str] = set()

    # Assets own their explicit descriptive passages. Build them first so the
    # same text is not also emitted as an unrelated text-only package.
    for unit_id in order:
        unit = graph.units[unit_id]
        if unit_id in covered or unit.type not in {"figure", "table"} or not _asset_is_useful(graph, unit_id):
            continue
        position = positions[unit_id]
        descriptions = [
            item for item in _asset_description_units(graph, unit_id, positions)
            if item not in covered
        ]
        core_ids = descriptions or [unit_id]
        support = [
            candidate for candidate in order[max(0, position-neighbor_radius):position+neighbor_radius+1]
            if candidate not in core_ids and graph.units[candidate].type in {"passage", "title"}
        ]
        packages.append(ContextPackage(
            id=f"context_{len(packages) + 1:06d}", core_unit_ids=core_ids,
            support_unit_ids=support, asset_part_ids=[unit_id],
            attributes={
                "package_type": "article_candidate", "candidate_sources": ["asset_anchor"],
                "section_path": unit.attributes.get("section_path", []),
                "heading_list_structure": {},
                "resource_title": (unit.attributes.get("resource_understanding") or {}).get("title"),
            },
        ))
        claimed.update([unit_id, *core_ids])

    for unit_id in order:
        unit = graph.units[unit_id]
        if unit_id in covered or unit_id in claimed or "noise" in unit.role:
            continue
        text = _unit_text(unit)
        sources: list[str] = []
        if unit.type in {"passage", "title"} and _CONCEPT_ANCHOR.search(text):
            sources.append("concept_anchor")
        if unit.type == "title" and _STRUCTURAL_HEADING.search(text.strip()):
            sources.append("heading_list_anchor")
        if unit.type == "passage" and _LIST_MARKER.search(text):
            sources.append("heading_list_anchor")
        if unit.type in {"passage", "title"}:
            sources.append("text_anchor")
        if not sources:
            continue
        position = positions[unit_id]
        core_ids = [unit_id]
        if unit.type == "title" and _STRUCTURAL_HEADING.search(text.strip()):
            list_ids = _continuous_list_units(graph, order, position)
            core_ids.extend(list_ids)
            claimed.update(list_ids)
        support = [
            candidate for candidate in order[max(0, position-neighbor_radius):position+neighbor_radius+1]
            if candidate not in core_ids and graph.units[candidate].type in {"passage", "title"}
        ]
        packages.append(ContextPackage(
            id=f"context_{len(packages) + 1:06d}", core_unit_ids=core_ids,
            support_unit_ids=support,
            asset_part_ids=[],
            attributes={
                "package_type": "article_candidate",
                "candidate_sources": _dedupe(sources),
                "section_path": unit.attributes.get("section_path", []),
                "heading_list_structure": _heading_list_structure(graph, order, position),
                "resource_title": (unit.attributes.get("resource_understanding") or {}).get("title"),
            },
        ))
        claimed.update(core_ids)


def _heading_list_structure(graph: DocumentGraph, order: list[str], position: int) -> dict:
    unit = graph.units[order[position]]
    text = _unit_text(unit).strip()
    heading = text if unit.type == "title" and _STRUCTURAL_HEADING.search(text) else None
    items: list[dict[str, str]] = []
    if heading:
        for unit_id in order[position + 1:position + 16]:
            candidate = graph.units[unit_id]
            value = _unit_text(candidate).strip()
            if candidate.type == "title":
                break
            if _LIST_MARKER.search(value):
                items.append({"unit_id": unit_id, "text": value})
            elif items:
                break
    elif _LIST_MARKER.search(text):
        items.append({"unit_id": unit.id, "text": text})
    return {"structural_heading": heading, "items": items, "evidence_unit_ids": [unit.id] + [i["unit_id"] for i in items]}


def _unit_text(unit) -> str:
    caption = unit.attributes.get("caption", "")
    body = unit.attributes.get("table_body", "") if unit.type == "table" else ""
    return "\n".join(str(value) for value in (unit.content, caption, body) if value)


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
        if unit.type == "formula"
    ]
    return sorted(formulas, key=lambda unit_id: position[unit_id])


def _adjacent_text_units(graph: DocumentGraph, formula_id: str, order: list[str]) -> list[str]:
    position = order.index(formula_id)
    section = graph.units[formula_id].attributes.get("section_path", [])
    result: list[str] = []
    for index in (position - 1, position + 1):
        if not 0 <= index < len(order):
            continue
        unit = graph.units[order[index]]
        if unit.type == "passage" and unit.attributes.get("section_path", []) == section:
            result.append(unit.id)
    return result


def _asset_is_useful(graph: DocumentGraph, asset_id: str) -> bool:
    unit = graph.units[asset_id]
    understanding = unit.attributes.get("resource_understanding") or {}
    if understanding.get("status") == "ok":
        return bool(understanding.get("useful"))
    if any(relation.target_id == asset_id and relation.type == "MENTIONS" for relation in graph.relations):
        return True
    if unit.type == "table" and str(unit.attributes.get("table_body") or "").strip():
        return True
    return bool(str(unit.attributes.get("caption") or "").strip())


def _asset_description_units(graph: DocumentGraph, asset_id: str, positions: dict[str, int]) -> list[str]:
    sources = {
        relation.source_id for relation in graph.relations
        if relation.target_id == asset_id and relation.type == "MENTIONS"
        and graph.units.get(relation.source_id) is not None
        and graph.units[relation.source_id].type in {"passage", "title"}
    }
    return sorted(sources, key=lambda item: positions.get(item, 10**9))


def _continuous_list_units(graph: DocumentGraph, order: list[str], position: int) -> list[str]:
    result: list[str] = []
    for unit_id in order[position + 1:position + 16]:
        unit = graph.units[unit_id]
        if unit.type == "title":
            break
        value = _unit_text(unit).strip()
        if _LIST_MARKER.search(value):
            result.append(unit_id)
        elif result:
            break
    return result


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
