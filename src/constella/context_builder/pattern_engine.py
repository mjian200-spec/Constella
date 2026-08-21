from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .models import Unit


@dataclass(slots=True)
class PatternMatch:
    pattern_id: str
    group: str
    matched_text: str
    span: list[int]
    captures: dict[str, str]
    action: str
    confidence: float
    config_version: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern_id": self.pattern_id, "group": self.group,
            "matched_text": self.matched_text, "span": self.span,
            "captures": self.captures, "action": self.action,
            "confidence": self.confidence, "config_version": self.config_version,
        }


class PatternEngine:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.version = int(config.get("version", 1))
        self.groups = config.get("groups", {})

    def validate(self) -> list[str]:
        errors: list[str] = []
        known_actions = {
            "add_role_candidate", "add_candidate", "create_relation_candidate",
            "create_asset_reference_candidate", "create_constraint_candidate",
            "mark_noise_candidate",
        }
        seen: set[str] = set()
        for group, rules in self.groups.items():
            for rule in rules:
                rule_id = rule.get("id")
                if not rule_id or rule_id in seen:
                    errors.append(f"duplicate or missing pattern id: {rule_id}")
                seen.add(rule_id)
                matcher = rule.get("matcher", {})
                if matcher.get("type") == "regex":
                    try:
                        re.compile(matcher["pattern"])
                    except (KeyError, re.error) as error:
                        errors.append(f"{rule_id}: invalid regex: {error}")
                if rule.get("action", {}).get("type") not in known_actions:
                    errors.append(f"{rule_id}: unknown action")
        return errors

    def match(self, group: str, unit: Unit) -> list[PatternMatch]:
        text = unit.content if isinstance(unit.content, str) else ""
        if not text:
            return []
        found: list[PatternMatch] = []
        for rule in self.groups.get(group, []):
            if not rule.get("enabled", True) or not self._allowed(rule, unit, text):
                continue
            found.extend(self._match_rule(group, rule, text))
        return found

    def match_all(self, unit: Unit) -> list[PatternMatch]:
        return [match for group in self.groups for match in self.match(group, unit)]

    def explain(self, unit: Unit) -> list[PatternMatch]:
        return self.match_all(unit)

    @staticmethod
    def _allowed(rule: dict[str, Any], unit: Unit, text: str) -> bool:
        conditions = rule.get("conditions", {})
        allowed_types = conditions.get("unit_types", [])
        if allowed_types and unit.type not in allowed_types:
            return False
        return conditions.get("min_length", 0) <= len(text) <= conditions.get("max_length", float("inf"))

    def _match_rule(self, group: str, rule: dict[str, Any], text: str) -> list[PatternMatch]:
        matcher, action = rule["matcher"], rule["action"]
        hits: list[tuple[str, int, int, dict[str, str]]] = []
        if matcher["type"] == "regex":
            flags = re.IGNORECASE if "IGNORECASE" in matcher.get("flags", []) else 0
            for hit in re.finditer(matcher["pattern"], text, flags):
                hits.append((hit.group(0), hit.start(), hit.end(), {k: v for k, v in hit.groupdict().items() if v is not None}))
        elif matcher["type"] == "keywords":
            values = matcher.get("values", [])
            if matcher.get("mode", "any") == "all" and not all(value in text for value in values):
                return []
            for value in values:
                start = text.find(value)
                if start >= 0:
                    hits.append((value, start, start + len(value), {}))
        return [PatternMatch(rule["id"], group, value, [start, end], captures,
                             action["type"], float(rule.get("confidence", 0.5)), self.version)
                for value, start, end, captures in hits]


def load_pattern_engine(config_path: str | Path) -> PatternEngine:
    with Path(config_path).open(encoding="utf-8") as handle:
        engine = PatternEngine(yaml.safe_load(handle))
    errors = engine.validate()
    if errors:
        raise ValueError("Invalid patterns configuration: " + "; ".join(errors))
    return engine
