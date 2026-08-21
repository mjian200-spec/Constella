from __future__ import annotations

from .models import DocumentGraph, PipelineRuntime
from .pattern_engine import PatternEngine


def finalize_routes(graph: DocumentGraph, runtime: PipelineRuntime, patterns: PatternEngine) -> None:
    for unit in graph.units.values():
        candidates = unit.attributes.setdefault("route_candidates", [])
        for match in patterns.match("rule_language", unit):
            candidates.append({"role": "rule", "pattern_id": match.pattern_id, "confidence": match.confidence})
        for match in patterns.match("ontology_language", unit):
            candidates.append({"role": "ontology", "pattern_id": match.pattern_id, "confidence": match.confidence})
        for candidate in candidates:
            if candidate["confidence"] >= 0.75 and candidate["role"] not in unit.role:
                unit.role.append(candidate["role"])
        if unit.type in {"title", "caption"} and unit.role == []:
            unit.role.append("support")
    runtime.record(stage="finalize_routes", rule_units=sum("rule" in unit.role for unit in graph.units.values()))
