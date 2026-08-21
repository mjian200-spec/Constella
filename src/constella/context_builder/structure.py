from __future__ import annotations

import re

from .cleaning import ordered_units
from .models import Ambiguity, DocumentGraph, PipelineRuntime
from .pattern_engine import PatternEngine


def build_document_structure(graph: DocumentGraph, runtime: PipelineRuntime, patterns: PatternEngine) -> None:
    stack: list[tuple[int, str, str]] = []
    previous_id: str | None = None
    for unit_id in ordered_units(graph):
        unit = graph.units[unit_id]
        if previous_id:
            graph.add_relation(previous_id, unit_id, "NEXT", confidence=1.0, evidence=["reading_order"])
        previous_id = unit_id
        level, confidence = _heading_level(unit)
        if level is not None:
            unit.type = "title"
            unit.attributes["heading_level"] = level
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, unit.id, str(unit.content)))
            if confidence < 0.8:
                graph.ambiguities[f"amb_heading_{unit.id}"] = Ambiguity(
                    f"amb_heading_{unit.id}", "heading_level", [unit.id], [],
                    "Heading level is inferred from weak layout evidence", "open",
                )
        if stack and unit.id != stack[-1][1]:
            graph.add_relation(unit.id, stack[-1][1], "IN_SECTION", confidence=1.0, evidence=[stack[-1][1]])
        unit.attributes["section_path"] = [title for _, _, title in stack]
    runtime.record(stage="build_document_structure", headings=sum(u.type == "title" for u in graph.units.values()))


def _heading_level(unit) -> tuple[int | None, float]:
    if not isinstance(unit.content, str):
        return None, 0.0
    text = unit.content.strip()
    numbered = re.match(r"^(\d+(?:[.．]\d+){1,3})\s+", text)
    chapter = re.match(r"^第\s*[0-9一二三四五六七八九十百]+\s*章", text)
    chinese = re.match(r"^[一二三四五六七八九十]+、", text)
    if numbered:
        return numbered.group(1).replace("．", ".").count(".") + 1, 0.95
    if chapter:
        return 1, 0.98
    if chinese:
        return 2, 0.9
    if unit.attributes.get("text_level") and len(text) < 100:
        return int(unit.attributes["text_level"]), 0.65
    return None, 0.0
