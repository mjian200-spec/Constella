from __future__ import annotations

from collections import defaultdict
import re

from .models import Ambiguity, Constraint, DocumentGraph, PipelineRuntime
from .pattern_engine import PatternEngine


def detect_and_align_conditions(graph: DocumentGraph, runtime: PipelineRuntime, patterns: PatternEngine) -> None:
    number = 1
    by_source: dict[str, list[Constraint]] = defaultdict(list)
    for unit in graph.units.values():
        if unit.type not in {"title", "passage", "formula"}:
            continue
        for match in patterns.match("condition_language", unit):
            value = match.captures.get("condition", match.matched_text).strip()
            kind = match.captures.get("kind") or _condition_type(value)
            constraint = Constraint(
                f"constraint_{number:06d}", kind, value, unit.id,
                {"scope_type": "pending", "start_unit_id": unit.id, "end_unit_id": None, "target_unit_ids": [], "candidate_ranges": []},
                "certain", {"pattern_id": match.pattern_id, "confidence": match.confidence, "kind_explicit": "kind" in match.captures},
            )
            graph.constraints[constraint.id] = constraint
            by_source[unit.id].append(constraint)
            number += 1
    _mark_same_statement_conflicts(graph, by_source)
    runtime.record(stage="detect_and_align_conditions", constraints=len(graph.constraints))


def _condition_type(value: str) -> str:
    for token in (
        "焊接电流", "电流", "电弧电压", "电压", "保护气体", "焊丝", "焊接位置",
        "接头", "材料", "母材", "板厚", "厚度", "弧长", "焊接速度", "送丝速度",
        "脉冲电流", "脉冲时间", "温度", "湿度", "间隙", "热输入", "频率", "极性", "试验",
    ):
        if token in value:
            return token
    if re.search(r"\d+(?:\.\d+)?\s*(?:g|MPa|L\s*/\s*min|A|V|mm|%)", value, re.I):
        return "parameter"
    return "explicit_condition"


def _mark_same_statement_conflicts(graph: DocumentGraph, by_source: dict[str, list[Constraint]]) -> None:
    """An ambiguity represents competing conditions, never an uncertain asset reference."""
    for source_id, constraints in by_source.items():
        source_text = graph.units[source_id].content
        if not isinstance(source_text, str) or len(source_text) > 120 or re.search(r"分别|相比|与.+不同|与.+相同|交流|直流", source_text):
            continue
        groups: dict[str, list[Constraint]] = defaultdict(list)
        for constraint in constraints:
            groups[constraint.type].append(constraint)
        for kind, siblings in groups.items():
            if kind in {"explicit_condition", "parameter"} or not all(item.attributes.get("kind_explicit") for item in siblings):
                continue
            values = {str(item.value) for item in siblings}
            if len(values) < 2:
                continue
            for item in siblings:
                item.status = "conflict"
            ambiguity_id = f"amb_conflict_{source_id}_{kind}"
            graph.ambiguities[ambiguity_id] = Ambiguity(
                ambiguity_id, "condition_conflict", [source_id], [item.id for item in siblings],
                f"Conflicting {kind} conditions occur in the same statement", "open",
            )
