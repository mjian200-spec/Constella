from __future__ import annotations

import re

from .cleaning import ordered_units
from .models import DocumentGraph, PipelineRuntime
from .pattern_engine import PatternEngine


def build_document_structure(graph: DocumentGraph, runtime: PipelineRuntime, patterns: PatternEngine) -> None:
    stack: list[tuple[int, str, str]] = []
    previous_id: str | None = None
    order = ordered_units(graph)
    accepted_numbered_ids = _accepted_numbered_heading_ids(graph, order)
    for unit_id in order:
        unit = graph.units[unit_id]
        if previous_id:
            graph.add_relation(previous_id, unit_id, "NEXT", confidence=1.0, evidence=["reading_order"])
        previous_id = unit_id
        level, confidence = _heading_level(unit, unit_id in accepted_numbered_ids)
        if level is not None:
            unit.type = "title"
            unit.attributes["heading_level"] = level
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, unit.id, str(unit.content)))
        if stack and unit.id != stack[-1][1]:
            graph.add_relation(unit.id, stack[-1][1], "IN_SECTION", confidence=1.0, evidence=[stack[-1][1]])
        unit.attributes["section_path"] = [title for _, _, title in stack]
    runtime.record(stage="build_document_structure", headings=sum(u.type == "title" for u in graph.units.values()))


def _heading_level(unit, numbered_is_supported: bool) -> tuple[int | None, float]:
    if unit.attributes.get("layout_role") or unit.attributes.get("mineru_type") == "aside_text":
        return None, 0.0
    if not isinstance(unit.content, str):
        return None, 0.0
    text = unit.content.strip()
    numbered = re.match(r"^(\d+(?:[.．]\d+){1,3})(?:\s+|$)", text)
    chapter = re.match(r"^第\s*[0-9一二三四五六七八九十百]+\s*章", text)
    chinese = re.match(r"^[一二三四五六七八九十]+、", text)
    if numbered and numbered_is_supported:
        return numbered.group(1).replace("．", ".").count(".") + 1, 0.95
    if chapter:
        return 1, 0.98
    if chinese and not _looks_like_ordinal_list(text):
        return 2, 0.9
    if unit.attributes.get("text_level") and len(text) < 100 and not _looks_like_ordinal_list(text):
        return int(unit.attributes["text_level"]), 0.65
    return None, 0.0


def _accepted_numbered_heading_ids(graph: DocumentGraph, order: list[str]) -> set[str]:
    """Accept numbered headings only when their hierarchy or sequence supports it."""
    candidates: list[tuple[int, str, tuple[int, ...]]] = []
    chapters: list[tuple[int, int]] = []
    for index, unit_id in enumerate(order):
        unit = graph.units[unit_id]
        if unit.attributes.get("layout_role") or not isinstance(unit.content, str):
            continue
        text = unit.content.strip()
        chapter = re.match(r"^第\s*(\d+)\s*章", text)
        if chapter:
            chapters.append((index, int(chapter.group(1))))
        match = re.match(r"^(\d+(?:[.．]\d+){1,3})(?:\s+|$)", text)
        all_numbers = re.findall(r"(?<!\d)\d+(?:[.．]\d+){1,3}(?!\d)", text)
        if match and len(all_numbers) == 1:
            candidates.append((index, unit_id, tuple(int(part) for part in match.group(1).replace("．", ".").split("."))))

    accepted: set[str] = set()
    for index, unit_id, number in candidates:
        parent, serial = number[:-1], number[-1]
        sibling_progression = any(
            other_number[:-1] == parent and abs(other_number[-1] - serial) == 1 and abs(other_index - index) <= 160
            for other_index, _, other_number in candidates if other_index != index
        )
        parent_exists = any(
            other_number == parent and other_index < index and index - other_index <= 160
            for other_index, _, other_number in candidates
        )
        preceding_chapter = next(
            (chapter_number for chapter_index, chapter_number in reversed(chapters) if chapter_index < index),
            None,
        )
        # A chapter can contain hundreds of OCR blocks. The current chapter,
        # rather than an arbitrary block-distance window, establishes a 2.2/
        # 3.3/5.4 heading's first number after the TOC has been removed.
        chapter_matches = len(number) == 2 and preceding_chapter == number[0]
        if sibling_progression or parent_exists or chapter_matches:
            accepted.add(unit_id)
    return accepted


def _looks_like_ordinal_list(text: str) -> bool:
    return bool(re.match(r"^(?:[（(]?\d+[）)]|\d+[.、])\s*", text))
