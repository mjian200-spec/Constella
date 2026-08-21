from __future__ import annotations

import re

from .models import Constraint, DocumentGraph, PipelineRuntime
from .pattern_engine import PatternEngine


def detect_and_align_conditions(graph: DocumentGraph, runtime: PipelineRuntime, patterns: PatternEngine) -> None:
    next_number = 1
    for unit in list(graph.units.values()):
        if unit.type not in {"title", "passage", "table_cell", "caption", "formula"}:
            continue
        text = unit.content if isinstance(unit.content, str) else ""
        matches = patterns.match("condition_language", unit)
        for match in matches:
            constraint_id = f"constraint_{next_number:06d}"; next_number += 1
            value = match.captures.get("condition", match.matched_text)
            graph.constraints[constraint_id] = Constraint(
                constraint_id, _condition_type(value), value, unit.id,
                {"scope_type": "pending", "start_unit_id": unit.id, "end_unit_id": None, "target_unit_ids": [], "candidate_ranges": []},
                "certain", {"pattern_id": match.pattern_id, "confidence": match.confidence},
            )
        if unit.type == "passage":
            _align_table_rows(graph, unit)
    _derive_table_conditions(graph, next_number)
    runtime.record(stage="detect_and_align_conditions", constraints=len(graph.constraints))


def _condition_type(value: str) -> str:
    if re.search(r"\d+(?:\.\d+)?\s*(?:g|MPa|L\s*/\s*min|A|V|mm|%)", value, re.I):
        return "parameter"
    if any(token in value for token in ("材料", "焊丝", "气体", "焊接", "试验")):
        return "process_condition"
    return "explicit_condition"


def _align_table_rows(graph: DocumentGraph, passage) -> None:
    mentions = {relation.target_id for relation in graph.relations if relation.source_id == passage.id and relation.type == "MENTIONS"}
    if not mentions:
        return
    for asset_id in mentions:
        rows = [unit for unit in graph.units.values() if unit.type == "table_row" and unit.attributes.get("asset_id") == asset_id]
        for row in rows:
            values = [value for value in row.content if isinstance(value, str)] if isinstance(row.content, list) else []
            row_numbers = {_normal_number(value) for value in values if re.fullmatch(r"\d+(?:\.\d+)?", value.strip())}
            passage_numbers = {_normal_number(value) for value in re.findall(r"(?<!\d)\d+(?:\.\d+)?(?!\d)", passage.content)}
            matched = sorted(row_numbers & passage_numbers)
            if matched:
                graph.add_relation(passage.id, row.id, "ALIGNS_WITH", confidence=0.95, evidence=[asset_id, row.id], attributes={"matched_values": matched})


def _normal_number(value: str) -> str:
    return format(float(value), "g")


def _derive_table_conditions(graph: DocumentGraph, start_number: int) -> None:
    """Create traceable row-local conditions from an explicit table header/value pair."""
    number = start_number
    for table in [unit for unit in graph.units.values() if unit.type == "table"]:
        rows = sorted(
            [unit for unit in graph.units.values() if unit.type == "table_row" and unit.attributes.get("asset_id") == table.id],
            key=lambda unit: unit.attributes["row_index"],
        )
        if len(rows) < 2 or not isinstance(rows[0].content, list):
            continue
        headers = rows[0].content
        for row in rows[1:]:
            if not isinstance(row.content, list):
                continue
            for column, value in enumerate(row.content):
                if column >= len(headers) or not re.fullmatch(r"\d+(?:\.\d+)?", value.strip()):
                    continue
                constraint_id = f"constraint_{number:06d}"; number += 1
                graph.constraints[constraint_id] = Constraint(
                    constraint_id, "table_condition", {"header": headers[column], "value": value}, row.id,
                    {"scope_type": "asset_parts", "start_unit_id": None, "end_unit_id": None, "target_unit_ids": [row.id], "candidate_ranges": []},
                    "certain", {"derived_from": [table.id, row.id], "method": "table_header_value"},
                )
