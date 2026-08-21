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
