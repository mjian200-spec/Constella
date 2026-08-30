from __future__ import annotations

from enum import StrEnum


SCHEMA_VERSION = "semantic_alignment.v2"


class ConceptType(StrEnum):
    OBJECT = "object"
    STATE = "state"


class StructureStatus(StrEnum):
    ATOMIC = "ATOMIC"
    COMPOSED = "COMPOSED"
    UNRESOLVED = "UNRESOLVED"


class AlignmentStatus(StrEnum):
    MATCHED = "MATCHED"
    PARTIAL = "PARTIAL"
    AMBIGUOUS = "AMBIGUOUS"
    PROPOSED = "PROPOSED"
    TYPE_REVIEW = "TYPE_REVIEW"
    EXPRESSION_ONLY = "EXPRESSION_ONLY"


class SemanticRole(StrEnum):
    RULE_VALUE = "RULE_VALUE"
    OBJECT_INTRINSIC_STATE = "OBJECT_INTRINSIC_STATE"
    RULE_CONDITION = "RULE_CONDITION"


class ProposalKind(StrEnum):
    CONCEPT_APPROVAL = "CONCEPT_APPROVAL"
    CONCEPT_MERGE = "CONCEPT_MERGE"
    OBJECT_CONCEPT = "OBJECT_CONCEPT"
    STATE_CONCEPT = "STATE_CONCEPT"
    NORMALIZATION_PATTERN = "NORMALIZATION_PATTERN"
    ALIAS = "ALIAS"
    TYPE_REVIEW = "TYPE_REVIEW"


class PackageTier(StrEnum):
    H0 = "H0"
    H1 = "H1"
    H2 = "H2"
    H3 = "H3"


class MatchMethod(StrEnum):
    EXACT_NAME = "EXACT_NAME"
    EXACT_ALIAS = "EXACT_ALIAS"
    LLM_CANDIDATE = "LLM_CANDIDATE"
    NONE = "NONE"


TIER_ORDER = {
    PackageTier.H0: 0,
    PackageTier.H1: 1,
    PackageTier.H2: 2,
    PackageTier.H3: 3,
}


def combine_alignment_statuses(statuses: list[str]) -> str:
    """Collapse component statuses without conflating structure and alignment."""
    if not statuses:
        return AlignmentStatus.EXPRESSION_ONLY
    values = set(statuses)
    if values == {AlignmentStatus.MATCHED}:
        return AlignmentStatus.MATCHED
    if AlignmentStatus.AMBIGUOUS in values:
        return AlignmentStatus.AMBIGUOUS
    if AlignmentStatus.TYPE_REVIEW in values:
        return AlignmentStatus.TYPE_REVIEW
    if AlignmentStatus.MATCHED in values:
        return AlignmentStatus.PARTIAL
    if AlignmentStatus.PROPOSED in values:
        return AlignmentStatus.PROPOSED
    return AlignmentStatus.EXPRESSION_ONLY
