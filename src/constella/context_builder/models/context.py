from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass(slots=True)
class ContextPackage:
    id: str
    core_unit_ids: list[str]
    support_unit_ids: list[str] = field(default_factory=list)
    constraint_ids: list[str] = field(default_factory=list)
    asset_part_ids: list[str] = field(default_factory=list)
    unresolved_ids: list[str] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
