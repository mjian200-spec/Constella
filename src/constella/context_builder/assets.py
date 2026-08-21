from __future__ import annotations

from html.parser import HTMLParser
import re

from .cleaning import ordered_units
from .models import Ambiguity, DocumentGraph, PipelineRuntime, SourceRef, Unit
from .pattern_engine import PatternEngine


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self.current_row: list[str] | None = None
        self.current_cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr": self.current_row = []
        if tag in {"td", "th"}: self.current_cell = []

    def handle_data(self, data):
        if self.current_cell is not None: self.current_cell.append(data)

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.current_row is not None and self.current_cell is not None:
            self.current_row.append("".join(self.current_cell).strip())
            self.current_cell = None
        if tag == "tr" and self.current_row is not None:
            self.rows.append(self.current_row)
            self.current_row = None


def build_asset_structure(graph: DocumentGraph, runtime: PipelineRuntime, patterns: PatternEngine) -> None:
    labels: dict[str, list[str]] = {}
    order = ordered_units(graph)
    for position, unit_id in enumerate(order):
        unit = graph.units[unit_id]
        if unit.type == "formula":
            _label_and_expand_formula(graph, unit, [graph.units[item] for item in order[max(0, position - 3):position]])
    for unit in list(graph.units.values()):
        label = unit.attributes.get("asset_label")
        if label:
            labels.setdefault(label, []).append(unit.id)
        if unit.type == "table":
            _expand_table(graph, unit)
        if unit.type in {"table", "figure"}:
            _add_caption(graph, unit)
    for unit_id in ordered_units(graph):
        unit = graph.units[unit_id]
        if unit.type not in {"passage", "title", "caption"} or not isinstance(unit.content, str):
            continue
        candidates = _references(unit.content)
        unit.attributes["asset_reference_candidates"] = candidates
        for candidate in candidates:
            matched = labels.get(candidate["label"], [])
            if len(matched) == 1:
                graph.add_relation(unit.id, matched[0], "MENTIONS", confidence=candidate["confidence"], evidence=[candidate["pattern_id"]])
                if graph.units[matched[0]].type == "formula":
                    graph.add_relation(unit.id, matched[0], "ALIGNS_WITH", confidence=candidate["confidence"], evidence=[candidate["pattern_id"]])
            elif len(matched) != 1:
                ambiguity_id = f"amb_asset_{unit.id}_{candidate['label']}"
                graph.ambiguities[ambiguity_id] = Ambiguity(
                    ambiguity_id, "asset_reference", [unit.id], matched,
                    f"Reference {candidate['label']} has {len(matched)} asset candidates", "open",
                )
        if re.search(r"曲线\s*\d+|区域\s*[ⅠⅡⅢIVX]+", unit.content):
            ambiguity_id = f"amb_image_parts_{unit.id}"
            graph.ambiguities[ambiguity_id] = Ambiguity(
                ambiguity_id, "figure_substructure_unavailable", [unit.id], [],
                "MinerU input has no reliable curve or region structure; image retained whole by project decision", "open",
            )
    runtime.record(stage="build_asset_structure", assets=sum(u.type in {"table", "figure", "formula"} for u in graph.units.values()))


def _references(text: str) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    for kind, token, pattern in (("figure", "图", r"图\s*(\d+)\s*[-—－]\s*(\d+)"), ("table", "表", r"表\s*(\d+)\s*[-—－]\s*(\d+)")):
        for hit in re.finditer(pattern, text):
            candidates.append({"asset_type": kind, "label": f"{token}{hit.group(1)}-{hit.group(2)}", "pattern_id": f"asset_reference.explicit_{kind}", "confidence": 0.99})
    for hit in re.finditer(r"式\s*[（(]?\s*(\d+)\s*[-—－]\s*(\d+)\s*[）)]?", text):
        candidates.append({"asset_type": "formula", "label": f"式{hit.group(1)}-{hit.group(2)}", "pattern_id": "asset_reference.explicit_formula", "confidence": 0.99})
    return candidates


def _expand_table(graph: DocumentGraph, table: Unit) -> None:
    body = table.attributes.get("table_body")
    if not isinstance(body, str) or not body.strip():
        return
    parser = _TableParser(); parser.feed(body)
    logical_rows = _split_repeated_table_groups(parser.rows)
    max_columns = max((len(row) for row in logical_rows), default=0)
    for column_index in range(max_columns):
        column_id = f"{table.id}_column_{column_index:02d}"
        values = [row[column_index] if column_index < len(row) else "" for row in logical_rows]
        graph.units[column_id] = Unit(column_id, "table_column", values, table.source, attributes={"column_index": column_index, "asset_id": table.id})
        graph.add_relation(table.id, column_id, "CONTAINS", confidence=1.0, evidence=[table.id])
    for row_index, values in enumerate(logical_rows):
        row_id = f"{table.id}_row_{row_index:02d}"
        row = Unit(row_id, "table_row", values, table.source, role=["support"] if row_index == 0 else [], attributes={"row_index": row_index, "asset_id": table.id})
        graph.units[row_id] = row
        graph.add_relation(table.id, row_id, "CONTAINS", confidence=1.0, evidence=[table.id])
        for column_index, value in enumerate(values):
            cell_id = f"{row_id}_cell_{column_index:02d}"
            cell = Unit(cell_id, "table_cell", value, table.source, attributes={"row_index": row_index, "column_index": column_index, "asset_id": table.id})
            graph.units[cell_id] = cell
            graph.add_relation(row_id, cell_id, "CONTAINS", confidence=1.0, evidence=[table.id])


def _split_repeated_table_groups(rows: list[list[str]]) -> list[list[str]]:
    """Expand side-by-side repeated table headers into independent logical rows."""
    if not rows or len(rows[0]) < 2 or len(rows[0]) % 2:
        return rows
    width = len(rows[0]) // 2
    if rows[0][:width] != rows[0][width:]:
        return rows
    logical = [rows[0][:width]]
    for row in rows[1:]:
        for offset in range(0, len(row), width):
            part = row[offset:offset + width]
            if part and any(value.strip() for value in part):
                logical.append(part)
    return logical


def _add_caption(graph: DocumentGraph, asset: Unit) -> None:
    caption = asset.attributes.get("caption")
    if not caption:
        return
    caption_id = f"{asset.id}_caption"
    graph.units[caption_id] = Unit(caption_id, "caption", caption, asset.source, role=["support"], attributes={"asset_id": asset.id})
    graph.add_relation(asset.id, caption_id, "CONTAINS", confidence=1.0, evidence=[asset.id])


def _label_and_expand_formula(graph: DocumentGraph, formula: Unit, previous_units: list[Unit]) -> None:
    labels = []
    for unit in previous_units:
        if isinstance(unit.content, str):
            labels.extend(candidate["label"] for candidate in _references(unit.content) if candidate["asset_type"] == "formula")
    if len(set(labels)) == 1:
        formula.attributes["asset_label"] = labels[0]
    expression = formula.content if isinstance(formula.content, str) else ""
    variables = sorted(set(re.findall(r"(?<![A-Za-z])([A-Za-z](?:_[A-Za-z0-9]+)?)(?![A-Za-z])", expression)))
    for index, variable in enumerate(variables):
        variable_id = f"{formula.id}_var_{index:02d}"
        graph.units[variable_id] = Unit(variable_id, "formula_variable", variable, formula.source, attributes={"formula_id": formula.id})
        graph.add_relation(formula.id, variable_id, "CONTAINS", confidence=1.0, evidence=[formula.id])
