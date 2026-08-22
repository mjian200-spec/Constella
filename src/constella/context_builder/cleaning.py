from __future__ import annotations

import re

from .models import DocumentGraph, PipelineRuntime
from .pattern_engine import PatternEngine


def ordered_units(graph: DocumentGraph) -> list[str]:
    return [unit.id for unit in sorted(
        graph.units.values(),
        key=lambda unit: (unit.source.page if unit.source.page is not None else -1,
                          (unit.source.bbox or [0, 0])[1], (unit.source.bbox or [0])[0], unit.id),
    )]


def normalize_document(graph: DocumentGraph, runtime: PipelineRuntime, patterns: PatternEngine) -> None:
    order = ordered_units(graph)
    graph.metadata["reading_order"] = order
    for index, unit_id in enumerate(order):
        unit = graph.units[unit_id]
        if isinstance(unit.content, str):
            unit.content = re.sub(r"(?<=\S)[ \t]*\n[ \t]*(?=\S)", "", unit.content).strip()
        matches = patterns.match_all(unit)
        unit.attributes["pattern_matches"] = [match.to_dict() for match in matches]
        unit.attributes["matched_pattern_ids"] = [match.pattern_id for match in matches]
        unit.attributes["route_candidates"] = []
        if unit.attributes.get("layout_role") in {"header", "footer", "page_number"}:
            unit.role.append("noise")
        for match in matches:
            if match.action == "add_role_candidate" and match.group == "noise":
                unit.attributes["route_candidates"].append({"role": "noise", "pattern_id": match.pattern_id, "confidence": match.confidence})
        if index:
            previous = graph.units[order[index - 1]]
            if _continues(previous, unit):
                graph.add_relation(previous.id, unit.id, "CONTINUES", confidence=0.8, evidence=["continuation.cross_page_sentence"])
    _remove_front_matter_before_body(graph, runtime)
    runtime.record(stage="normalize_document", units=len(graph.units))


def _continues(previous, current) -> bool:
    if previous.type != "passage" or current.type != "passage":
        return False
    if previous.source.page is None or current.source.page != previous.source.page + 1:
        return False
    if previous.attributes.get("layout_role") or current.attributes.get("layout_role"):
        return False
    if not isinstance(previous.content, str) or not isinstance(current.content, str):
        return False
    return bool(previous.content and current.content and previous.content[-1] not in "。！？；:：" and current.content[0] not in "第图表")


def _remove_front_matter_before_body(graph: DocumentGraph, runtime: PipelineRuntime) -> None:
    """Remove a table of contents and everything before the body it indexes.

    A TOC contains its own chapter headings, so the body boundary is not the
    first ``第1章`` after ``目录``.  We require a chapter-number reset: after the
    TOC has reached a later chapter, a new non-header ``第1章`` starts the body.
    If that evidence is absent, retain the document rather than guess.
    """
    order = ordered_units(graph)
    toc_positions = [
        index for index, unit_id in enumerate(order)
        if isinstance(graph.units[unit_id].content, str)
        and graph.units[unit_id].content.strip().replace(" ", "") == "目录"
    ]
    if not toc_positions:
        return
    toc_position = toc_positions[-1]
    chapters: list[tuple[int, int]] = []
    for index, unit_id in enumerate(order[toc_position + 1:], start=toc_position + 1):
        unit = graph.units[unit_id]
        if unit.attributes.get("layout_role") or not isinstance(unit.content, str):
            continue
        match = re.match(r"^第\s*(?P<number>\d+)\s*章", unit.content.strip())
        if match:
            chapters.append((index, int(match.group("number"))))
    body_starts = [
        index for index, number in chapters
        if number == 1 and any(previous_number > 1 for previous_index, previous_number in chapters if previous_index < index)
    ]
    if not body_starts:
        return
    body_start = body_starts[-1]
    removed_ids = set(order[:body_start])
    graph.units = {unit_id: unit for unit_id, unit in graph.units.items() if unit_id not in removed_ids}
    graph.relations = [
        relation for relation in graph.relations
        if relation.source_id not in removed_ids and relation.target_id not in removed_ids
    ]
    graph.metadata["front_matter"] = {
        "removed_unit_count": len(removed_ids),
        "toc_unit_id": order[toc_position],
        "body_start_unit_id": order[body_start],
    }
    runtime.record(stage="remove_front_matter", removed_units=len(removed_ids), body_start=order[body_start])
