from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass(slots=True)
class SourceRef:
    page: int | None = None
    bbox: list[float] | None = None
    original_block_id: str | None = None
    asset_path: str | None = None


@dataclass(slots=True)
class Unit:
    id: str
    type: str
    content: str | dict[str, Any] | list[Any] | None
    source: SourceRef
    role: list[str] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Relation:
    id: str
    source_id: str
    target_id: str
    type: str
    confidence: float | None = None
    evidence: list[str] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Constraint:
    id: str
    type: str
    value: Any
    source_id: str
    scope: dict[str, Any]
    status: str = "certain"
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Ambiguity:
    id: str
    type: str
    source_unit_ids: list[str]
    candidate_ids: list[str]
    reason: str
    status: str = "open"


@dataclass(slots=True)
class DocumentGraph:
    units: dict[str, Unit] = field(default_factory=dict)
    relations: list[Relation] = field(default_factory=list)
    constraints: dict[str, Constraint] = field(default_factory=dict)
    ambiguities: dict[str, Ambiguity] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def add_relation(
        self, source_id: str, target_id: str, relation_type: str,
        *, confidence: float | None = None, evidence: list[str] | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Relation:
        relation = Relation(
            id=f"rel_{len(self.relations) + 1:06d}", source_id=source_id,
            target_id=target_id, type=relation_type, confidence=confidence,
            evidence=evidence or [], attributes=attributes or {},
        )
        self.relations.append(relation)
        return relation

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
