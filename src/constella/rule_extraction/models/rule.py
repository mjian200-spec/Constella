from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass(slots=True)
class StateExpression:
    id: str
    object: str
    raw_state: str
    normalized_state: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class StateTransition:
    object: str
    from_state_id: str
    to_state_id: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class StructuredRule:
    id: str
    context_package_id: str
    rule_group_id: str
    rule_index: int
    conditions: list[StateExpression]
    antecedents: list[StateExpression]
    consequents: list[StateExpression]
    relation: str | None
    transitions: list[StateTransition]
    raw_expression: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class StructuredRuleSet:
    context_package_id: str
    rules: list[StructuredRule]
    final_expression: str
    prompt_id: str
    prompt_version: str
    model: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class PackageProcessingResult:
    context_package_id: str
    status: str
    rule_ids: list[str] = field(default_factory=list)
    failure_stage: str | None = None
    failure_code: str | None = None
    failure_reason: str | None = None
    input_fingerprint: str = ""
    run_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)
