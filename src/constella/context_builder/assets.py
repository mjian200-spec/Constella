from __future__ import annotations

import re
from difflib import SequenceMatcher

from .cleaning import ordered_units
from .models import DocumentGraph, PipelineRuntime
from .pattern_engine import PatternEngine


ASSET_TYPES = {"figure", "table", "formula"}


def build_asset_structure(graph: DocumentGraph, runtime: PipelineRuntime, patterns: PatternEngine) -> None:
    """Link text to complete assets only; no table cells or image subparts are invented."""
    order = ordered_units(graph)
    _label_formulas(graph, order)
    labels = _asset_labels(graph)
    positions = {unit_id: index for index, unit_id in enumerate(order)}
    for unit_id in order:
        unit = graph.units[unit_id]
        if unit.type not in {"passage", "title"} or not isinstance(unit.content, str):
            continue
        candidates = _references(unit.content)
        unit.attributes["asset_reference_candidates"] = candidates
        for candidate in candidates:
            matches = labels.get(candidate["label"], [])
            if len(matches) == 1:
                _link(graph, unit.id, matches[0], candidate["confidence"], candidate["pattern_id"])
        _link_introduced_formula(graph, unit, order, positions)
        _link_relative_reference(graph, unit, order, positions)
        _link_caption_description(graph, unit)
    runtime.record(stage="build_asset_structure", assets=sum(unit.type in ASSET_TYPES for unit in graph.units.values()))


def _asset_labels(graph: DocumentGraph) -> dict[str, list[str]]:
    labels: dict[str, list[str]] = {}
    for unit in graph.units.values():
        label = unit.attributes.get("asset_label")
        if label:
            labels.setdefault(label, []).append(unit.id)
    return labels


def _label_formulas(graph: DocumentGraph, order: list[str]) -> None:
    """Label formulas from their own equation tag, not from a nearby citation.

    A passage usually cites an equation *before* it appears.  Using that
    citation to label the next formula shifts labels whenever a paragraph
    cites one equation and then introduces another.  The equation's ``tag``
    is the authoritative source whenever MinerU preserved it.
    """
    for position, unit_id in enumerate(order):
        formula = graph.units[unit_id]
        if formula.type != "formula":
            continue
        label = _formula_tag(str(formula.content or ""))
        if label:
            formula.attributes["asset_label"] = label
            formula.attributes["asset_label_source"] = "equation_tag"
            _link_symbol_explanations(graph, formula.id, order, position)
            continue
        # Some OCR equations have no recoverable tag.  Keep the conservative
        # local fallback, but make its provenance visible for review.
        labels: set[str] = set()
        for previous_id in order[max(0, position - 3):position]:
            previous = graph.units[previous_id]
            if isinstance(previous.content, str):
                labels.update(item["label"] for item in _references(previous.content) if item["asset_type"] == "formula")
        if len(labels) == 1:
            formula.attributes["asset_label"] = labels.pop()
            formula.attributes["asset_label_source"] = "nearby_reference_fallback"
        _link_symbol_explanations(graph, formula.id, order, position)


def _formula_tag(text: str) -> str | None:
    hit = re.search(r"\\tag\s*\{?\s*(\d+)\s*[-—－]\s*(\d+)\s*\}?", text)
    return f"式{hit.group(1)}-{hit.group(2)}" if hit else None


def _link_symbol_explanations(graph: DocumentGraph, formula_id: str, order: list[str], position: int) -> None:
    """Link the contiguous, source-text symbol glossary following a formula.

    These are retained as passages; this relation merely records that they
    explain the complete formula.  It does not claim to parse variables.
    """
    for unit_id in order[position + 1:]:
        unit = graph.units[unit_id]
        if unit.type == "title":
            break
        if unit.type != "passage":
            continue
        text = str(unit.content or "").strip()
        if _is_symbol_explanation(text):
            graph.add_relation(
                formula_id, unit_id, "EXPLAINED_BY", confidence=0.99,
                evidence=["formula.symbol_explanation"],
            )
            continue
        if text and "noise" not in unit.role:
            break


def _is_symbol_explanation(text: str) -> bool:
    return bool(re.match(r"^(?:式中\s*)?.{1,120}?(?:——|—|-{2,})", text) or re.match(r"^其他符号.{0,40}式", text))


def _references(text: str) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    for kind, token, pattern in (
        ("figure", "图", r"图\s*(\d+)\s*[-—－]\s*(\d+)"),
        ("table", "表", r"表\s*(\d+)\s*[-—－]\s*(\d+)"),
        ("formula", "式", r"式\s*[（(]?\s*(\d+)\s*[-—－]\s*(\d+)\s*[）)]?"),
    ):
        for hit in re.finditer(pattern, text):
            candidates.append({"asset_type": kind, "label": f"{token}{hit.group(1)}-{hit.group(2)}", "pattern_id": f"asset_reference.explicit_{kind}", "confidence": 0.99})
    return candidates


def _link_relative_reference(graph: DocumentGraph, unit, order: list[str], positions: dict[str, int]) -> None:
    for direction, pattern, expected_type in (
        (-1, r"(?:上图|前图|上述图)", "figure"), (1, r"(?:下图|后图)", "figure"),
        (-1, r"(?:上表|前表|上述表)", "table"), (1, r"(?:下表|后表)", "table"),
    ):
        if not re.search(pattern, unit.content):
            continue
        candidate = _nearest_asset(graph, unit.id, order, positions, direction, expected_type)
        if candidate:
            _link(graph, unit.id, candidate, 0.7, "asset_reference.relative")


def _link_introduced_formula(graph: DocumentGraph, unit, order: list[str], positions: dict[str, int]) -> None:
    """Link a formula explicitly introduced by the immediately preceding text.

    Engineering prose often ends with “其关系式为” and places the equation in
    the next MinerU block, without repeating an equation number.  This is a
    source-local structural cue, not an inferred mathematical relationship.
    """
    text = str(unit.content or "").strip()
    if not re.search(r"(?:关系式|表达式|方程|公式|计算式)(?:如下|为|是)?[：:]?$", text):
        return
    index = positions[unit.id] + 1
    source_path = unit.attributes.get("section_path", [])
    while index < len(order):
        candidate = graph.units[order[index]]
        if candidate.type == "title" or candidate.attributes.get("section_path", []) != source_path:
            return
        if candidate.type == "formula":
            graph.add_relation(
                unit.id, candidate.id, "INTRODUCES", confidence=0.98,
                evidence=["asset_reference.introduced_formula"],
            )
            return
        if candidate.type == "passage" and str(candidate.content or "").strip() and "noise" not in candidate.role:
            return
        index += 1


def _nearest_asset(graph: DocumentGraph, source_id: str, order: list[str], positions: dict[str, int], direction: int, expected_type: str) -> str | None:
    source_path = graph.units[source_id].attributes.get("section_path", [])
    index = positions[source_id] + direction
    while 0 <= index < len(order):
        candidate = graph.units[order[index]]
        if candidate.type == expected_type:
            return candidate.id if candidate.attributes.get("section_path", []) == source_path else None
        if candidate.type == "title" and candidate.attributes.get("section_path", []) != source_path:
            return None
        index += direction
    return None


def _link_caption_description(graph: DocumentGraph, unit) -> None:
    """Match descriptive text to one clearly superior caption; otherwise retain candidates only."""
    if _has_asset_link(graph, unit.id):
        return
    source_text = _normalise(unit.content)
    if len(source_text) < 8:
        return
    source_path = unit.attributes.get("section_path", [])
    scored: list[tuple[float, str]] = []
    for asset in graph.units.values():
        if asset.type not in {"figure", "table"} or asset.attributes.get("section_path", []) != source_path:
            continue
        caption = _normalise(str(asset.attributes.get("caption", "")))
        if caption:
            score = SequenceMatcher(None, source_text, caption).ratio()
            if score >= 0.65:
                scored.append((score, asset.id))
    scored.sort(reverse=True)
    unit.attributes["caption_match_candidates"] = [{"asset_id": asset_id, "score": round(score, 3)} for score, asset_id in scored[:3]]
    if len(scored) == 1 or (len(scored) > 1 and scored[0][0] - scored[1][0] >= 0.2):
        _link(graph, unit.id, scored[0][1], scored[0][0], "asset_reference.caption_description")


def _normalise(text: str) -> str:
    return re.sub(r"[\W_图表式]", "", text).lower()


def _has_asset_link(graph: DocumentGraph, source_id: str, target_id: str | None = None) -> bool:
    return any(
        relation.source_id == source_id and relation.type == "MENTIONS"
        and (target_id is None or relation.target_id == target_id)
        for relation in graph.relations
    )


def _link(graph: DocumentGraph, source_id: str, target_id: str, confidence: float, evidence: str) -> None:
    if not _has_asset_link(graph, source_id, target_id):
        graph.add_relation(source_id, target_id, "MENTIONS", confidence=confidence, evidence=[evidence])
    if graph.units[target_id].type == "formula":
        graph.add_relation(source_id, target_id, "ALIGNS_WITH", confidence=confidence, evidence=[evidence])
