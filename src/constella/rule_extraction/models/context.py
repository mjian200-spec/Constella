from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass(slots=True)
class ResolvedUnit:
    id: str
    type: str
    content: str | dict[str, Any] | list[Any] | None
    source: dict[str, Any]
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ResolvedConstraint:
    id: str
    type: str
    value: Any
    source_id: str
    scope: dict[str, Any]
    status: str
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ResolvedAsset:
    unit: ResolvedUnit
    original_path: str | None
    resolved_path: str | None
    caption: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ResolvedContextPackage:
    id: str
    core_units: list[ResolvedUnit]
    support_units: list[ResolvedUnit]
    constraints: list[ResolvedConstraint]
    assets: list[ResolvedAsset]
    unresolved: list[dict[str, Any]]
    section_path: list[str]
    source_package: dict[str, Any]
    source_fingerprint: str
    resolver_version: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
