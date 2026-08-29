from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable


class ConceptInputError(ValueError):
    """Concept-layer inputs do not satisfy the expected upstream contract."""


def stable_hash(prefix: str, *parts: str, length: int = 16) -> str:
    payload = "\x1f".join(parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()[:length]}"


def fingerprint(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ConceptInputIndex:
    def __init__(
        self,
        graph: dict[str, Any],
        packages: list[dict[str, Any]],
        rulesets: list[dict[str, Any]],
    ) -> None:
        self.graph = graph
        self.units: dict[str, dict[str, Any]] = dict(graph.get("units") or {})
        self.packages = {str(item["id"]): item for item in packages}
        self.rulesets = rulesets
        raw_order = list((graph.get("metadata") or {}).get("reading_order") or [])
        self.reading_order = [unit_id for unit_id in raw_order if unit_id in self.units]
        if not self.reading_order:
            self.reading_order = list(self.units)
        self.positions = {unit_id: index for index, unit_id in enumerate(self.reading_order)}
        self.section_units: dict[tuple[str, ...], list[str]] = {}
        for unit_id in self.reading_order:
            section = tuple(self.unit_section(unit_id))
            self.section_units.setdefault(section, []).append(unit_id)
        self.input_fingerprint = fingerprint({
            "graph": graph,
            "packages": packages,
            "rulesets": rulesets,
        })

    @classmethod
    def load(
        cls,
        context_output_dir: str | Path,
        rule_output_dir: str | Path,
        *,
        rule_ids: set[str] | None = None,
        limit: int | None = None,
    ) -> "ConceptInputIndex":
        context_dir = Path(context_output_dir)
        rule_dir = Path(rule_output_dir)
        graph_path = context_dir / "document_graph.json"
        package_path = context_dir / "context_packages.jsonl"
        if not graph_path.is_file() or not package_path.is_file():
            raise ConceptInputError(f"Missing Context Builder outputs in {context_dir}")
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        packages = _read_jsonl(package_path)
        rulesets = _load_rulesets(rule_dir)
        if rule_ids:
            filtered: list[dict[str, Any]] = []
            for ruleset in rulesets:
                rules = [rule for rule in list(ruleset.get("rules") or []) if str(rule.get("id")) in rule_ids]
                if rules:
                    filtered.append({**ruleset, "rules": rules})
            rulesets = filtered
        if limit is not None:
            rulesets = _limit_rules(rulesets, limit)
        return cls(graph, packages, rulesets)

    def iter_rules(self) -> Iterable[dict[str, Any]]:
        for ruleset in self.rulesets:
            for rule in ruleset.get("rules") or []:
                yield rule

    def package_unit_ids(self, package_id: str) -> list[str]:
        package = self.packages.get(package_id) or {}
        result: list[str] = []
        for key in ("core_unit_ids", "support_unit_ids", "asset_part_ids"):
            result.extend(str(item) for item in package.get(key) or [])
        constraint_ids = package.get("constraint_ids") or []
        constraints = self.graph.get("constraints") or {}
        result.extend(
            str(constraints[item]["source_id"])
            for item in constraint_ids
            if item in constraints and constraints[item].get("source_id")
        )
        return list(dict.fromkeys(unit_id for unit_id in result if unit_id in self.units))

    def package_section(self, package_id: str) -> list[str]:
        package = self.packages.get(package_id) or {}
        return list((package.get("attributes") or {}).get("section_path") or [])

    def unit_text(self, unit_id: str) -> str:
        value = (self.units.get(unit_id) or {}).get("content")
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False) if value is not None else ""

    def unit_section(self, unit_id: str) -> list[str]:
        return list(((self.units.get(unit_id) or {}).get("attributes") or {}).get("section_path") or [])

    def unit_page(self, unit_id: str) -> int | None:
        value = ((self.units.get(unit_id) or {}).get("source") or {}).get("page")
        return int(value) if isinstance(value, int) else None

    def section_neighbor_ids(self, package_id: str, radius: int = 8) -> list[str]:
        package_ids = self.package_unit_ids(package_id)
        if not package_ids:
            return []
        section = tuple(self.package_section(package_id))
        section_ids = self.section_units.get(section, [])
        if not section_ids:
            return []
        positions = [section_ids.index(item) for item in package_ids if item in section_ids]
        if not positions:
            return []
        start, end = max(0, min(positions) - radius), min(len(section_ids), max(positions) + radius + 1)
        return section_ids[start:end]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ConceptInputError(f"Invalid JSONL at {path}:{line_number}") from error
        if not isinstance(value, dict):
            raise ConceptInputError(f"Expected object at {path}:{line_number}")
        result.append(value)
    return result


def _load_rulesets(rule_dir: Path) -> list[dict[str, Any]]:
    # ``structured_rules.jsonl`` is atomically rebuilt from the package states
    # belonging to the current extraction run.  Per-package files are caches and
    # can legitimately contain leftovers after a limited/reset run, so only use
    # them for backwards compatibility when the authoritative export is absent.
    flat_path = rule_dir / "structured_rules.jsonl"
    if flat_path.is_file():
        grouped: dict[str, list[dict[str, Any]]] = {}
        for rule in _read_jsonl(flat_path):
            grouped.setdefault(str(rule.get("context_package_id", "")), []).append(rule)
        return [{"context_package_id": package_id, "rules": rules} for package_id, rules in sorted(grouped.items())]

    ruleset_dir = rule_dir / "rulesets"
    if ruleset_dir.is_dir():
        result = []
        for path in sorted(ruleset_dir.glob("*.json")):
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                result.append(value)
        if result:
            return result
    raise ConceptInputError(f"No rulesets or structured_rules.jsonl in {rule_dir}")


def _limit_rules(rulesets: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    remaining = max(0, limit)
    result: list[dict[str, Any]] = []
    for ruleset in rulesets:
        if remaining <= 0:
            break
        rules = list(ruleset.get("rules") or [])[:remaining]
        if rules:
            result.append({**ruleset, "rules": rules})
            remaining -= len(rules)
    return result
