from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator

from .models import ResolvedAsset, ResolvedConstraint, ResolvedContextPackage, ResolvedUnit


RESOLVER_VERSION = "1"


class InputResolutionError(ValueError):
    """A context package cannot be expanded faithfully from Context Builder output."""


def _fingerprint(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _file_fingerprint(path_value: str | None) -> str | None:
    if not path_value:
        return None
    path = Path(path_value)
    if not path.is_file():
        return "missing"
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DocumentGraphIndex:
    def __init__(self, graph: dict[str, Any], graph_path: Path, asset_root: Path | None = None) -> None:
        self.graph = graph
        self.graph_path = graph_path
        self.units = graph.get("units", {})
        self.constraints = graph.get("constraints", {})
        self.ambiguities = graph.get("ambiguities", {})
        self.fingerprint = _fingerprint(graph)
        raw_input = graph.get("metadata", {}).get("input_path")
        input_path = Path(raw_input) if raw_input else None
        if input_path and not input_path.is_absolute():
            input_path = (Path.cwd() / input_path).resolve()
        self.input_dir = input_path.parent if input_path else graph_path.parent
        self.asset_root = asset_root.resolve() if asset_root else None

    @classmethod
    def load(cls, path: str | Path, asset_root: str | Path | None = None) -> "DocumentGraphIndex":
        graph_path = Path(path).resolve()
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        if not isinstance(graph, dict) or not isinstance(graph.get("units"), dict):
            raise InputResolutionError(f"Invalid document graph: {graph_path}")
        return cls(graph, graph_path, Path(asset_root) if asset_root else None)

    def unit(self, unit_id: str) -> ResolvedUnit:
        raw = self.units.get(unit_id)
        if not isinstance(raw, dict):
            raise InputResolutionError(f"Missing unit reference: {unit_id}")
        return ResolvedUnit(
            id=unit_id, type=str(raw.get("type", "")), content=raw.get("content"),
            source=dict(raw.get("source") or {}), attributes=dict(raw.get("attributes") or {}),
        )

    def constraint(self, constraint_id: str) -> ResolvedConstraint:
        raw = self.constraints.get(constraint_id)
        if not isinstance(raw, dict):
            raise InputResolutionError(f"Missing constraint reference: {constraint_id}")
        return ResolvedConstraint(
            id=constraint_id, type=str(raw.get("type", "")), value=raw.get("value"),
            source_id=str(raw.get("source_id", "")), scope=dict(raw.get("scope") or {}),
            status=str(raw.get("status", "certain")), attributes=dict(raw.get("attributes") or {}),
        )

    def unresolved(self, ambiguity_id: str) -> dict[str, Any]:
        raw = self.ambiguities.get(ambiguity_id)
        if not isinstance(raw, dict):
            raise InputResolutionError(f"Missing ambiguity reference: {ambiguity_id}")
        return dict(raw)

    def resolve_asset_path(self, original_path: str | None) -> str | None:
        if not original_path:
            return None
        path = Path(original_path)
        candidates = [path] if path.is_absolute() else [self.input_dir / path, self.graph_path.parent / path]
        if self.asset_root is not None and not path.is_absolute():
            candidates.append(self.asset_root / path)
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate.resolve())
        return None


def iter_packages(path: str | Path) -> Iterator[dict[str, Any]]:
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            package = json.loads(line)
        except json.JSONDecodeError as error:
            raise InputResolutionError(f"Invalid context package JSON at line {line_number}") from error
        if not isinstance(package, dict) or not package.get("id"):
            raise InputResolutionError(f"Context package at line {line_number} has no id")
        yield package


def resolve_package(index: DocumentGraphIndex, package: dict[str, Any]) -> ResolvedContextPackage:
    def unique(ids: list[str]) -> list[str]:
        return list(dict.fromkeys(ids))

    core = [index.unit(unit_id) for unit_id in unique(list(package.get("core_unit_ids") or []))]
    support = [index.unit(unit_id) for unit_id in unique(list(package.get("support_unit_ids") or []))]
    constraints = [index.constraint(item_id) for item_id in unique(list(package.get("constraint_ids") or []))]
    assets: list[ResolvedAsset] = []
    for unit_id in unique(list(package.get("asset_part_ids") or [])):
        unit = index.unit(unit_id)
        original_path = unit.source.get("asset_path")
        assets.append(ResolvedAsset(
            unit=unit, original_path=original_path, resolved_path=index.resolve_asset_path(original_path),
            caption=unit.attributes.get("caption") or (str(unit.content) if unit.type == "figure" else None),
        ))
    unresolved = [index.unresolved(item_id) for item_id in unique(list(package.get("unresolved_ids") or []))]
    if not core:
        raise InputResolutionError(f"Context package {package['id']} has no core units")
    source_fingerprint = _fingerprint({
        "graph": index.fingerprint, "package": package, "resolver": RESOLVER_VERSION,
        "assets": [{"original_path": asset.original_path, "file": _file_fingerprint(asset.resolved_path)} for asset in assets],
    })
    return ResolvedContextPackage(
        id=str(package["id"]), core_units=core, support_units=support, constraints=constraints, assets=assets,
        unresolved=unresolved, section_path=list((package.get("attributes") or {}).get("section_path") or []),
        source_package=package, source_fingerprint=source_fingerprint, resolver_version=RESOLVER_VERSION,
    )


def package_as_dict(package: ResolvedContextPackage) -> dict[str, Any]:
    return asdict(package)
