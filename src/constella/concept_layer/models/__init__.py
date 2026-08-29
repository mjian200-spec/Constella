from __future__ import annotations
from dataclasses import asdict, dataclass
from typing import Any

class Serializable:
    def to_dict(self) -> dict[str, Any]: return asdict(self)

@dataclass(slots=True)
class RuleObjectRef(Serializable):
    rule_id: str; context_package_id: str; state_expression_id: str; side: str; position: int; raw_object: str

@dataclass(slots=True)
class ObjectSeed(Serializable):
    seed_id: str; raw_name: str; normalized_name: str; rule_refs: list[RuleObjectRef]

@dataclass(slots=True)
class EvidenceItem(Serializable):
    evidence_id: str; unit_id: str; text: str; section_path: list[str]; page: int | None; retrieval_methods: list[str]

@dataclass(slots=True)
class ConceptEvidenceBundle(Serializable):
    seed_id: str; seed_name: str; evidence: list[EvidenceItem]; retrieval_status: str

@dataclass(slots=True)
class ParentProposal(Serializable):
    name: str; relation_type: str; directness: str; evidence_ids: list[str]; definition: str | None = None

@dataclass(slots=True)
class ParentDecision(Serializable):
    name: str; relation_type: str; decision: str; directness: str
    evidence_ids: list[str]; reason: str; definition: str | None = None

@dataclass(slots=True)
class ConceptResolution(Serializable):
    seed_id: str; decision: str; canonical_name: str | None; definition: str | None
    definition_type: str | None; definition_evidence_ids: list[str]; aliases: list[str]
    parent_proposals: list[ParentProposal]; parent_decisions: list[ParentDecision]
    evidence_ids: list[str]; reason: str
    prompt_id: str | None = None; prompt_version: str | None = None
    configured_model: str | None = None; served_model: str | None = None
    qualification_reason: str | None = None
    definition_status: str | None = None; definition_reason: str | None = None
    enrichment_prompt_id: str | None = None; enrichment_prompt_version: str | None = None

@dataclass(slots=True)
class Concept(Serializable):
    concept_id: str; canonical_name: str; definition: str | None; definition_type: str | None
    aliases: list[str]; source_seed_ids: list[str]; evidence_ids: list[str]; origin_depth: int
    source_package_ids: list[str] | None = None
    audit_status: str | None = None

@dataclass(slots=True)
class RuleConceptBinding(Serializable):
    binding_id: str; rule_id: str; state_expression_id: str; side: str; position: int
    field: str; raw_object: str; concept_id: str; evidence_ids: list[str]

@dataclass(slots=True)
class ConceptRelation(Serializable):
    relation_id: str; child_concept_id: str; type: str; parent_concept_id: str
    directness: str; evidence_ids: list[str]
    source_package_ids: list[str] | None = None
    original_relation: dict[str, Any] | None = None
    audit_status: str | None = None

@dataclass(slots=True)
class ProcessingResult(Serializable):
    entity_type: str; entity_id: str; status: str; reason: str | None = None
