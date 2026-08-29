from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Any, Callable
import yaml
from constella.context_builder.llm_client import LLMClient
from .models import ConceptEvidenceBundle, ConceptResolution, ObjectSeed, ParentDecision, ParentProposal
from .object_seeds import normalize_name

class ResolutionError(ValueError): pass

class ConceptResolver:
    """Decide eligibility first; enrichment can never revoke an accepted concept."""
    def __init__(self,models:dict[str,Any],model_key:str,prompt_path:str|Path,client=None)->None:
        self.models=models; self.model_key=model_key; self.client=client or LLMClient(models)
        prompt_dir=Path(prompt_path).parent
        self.qualification_prompt=self._load(prompt_path)
        self.enrichment_prompt=self._load(prompt_dir/"concept_enrichment_v1.yaml")
        self.relation_prompt=self._load(prompt_dir/"relation_classifier_v1.yaml")
        self.directness_prompt=self._load(prompt_dir/"direct_parent_resolver_v1.yaml")

    @staticmethod
    def _load(path):
        value=yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not {"id","version","system"}.issubset(value): raise ValueError(f"Invalid prompt: {path}")
        return value

    def resolve(self,seed:ObjectSeed,bundle:ConceptEvidenceBundle)->ConceptResolution:
        allowed={e.evidence_id for e in bundle.evidence}
        payload={"seed":seed.to_dict(),"evidence":[e.to_dict() for e in bundle.evidence]}
        qualification,response,errors=self._call(self.qualification_prompt,payload,lambda v:self._validate_qualification(v,allowed,bundle,seed))
        if qualification is None:
            return self._base(seed,"ambiguous",None,[],allowed,"qualification_validation_failed: "+" | ".join(errors),response,"not_run")
        decision=qualification["decision"]; reason=qualification.get("reason","")
        canonical=self._safe_canonical(qualification.get("canonical_name"),seed,bundle) if decision=="accepted" else qualification.get("canonical_name")
        aliases=self._valid_aliases(qualification.get("aliases",[]),bundle)
        if decision!="accepted":
            return self._base(seed,decision,canonical,aliases,set(qualification.get("evidence_ids",[])),reason,response,"not_applicable")

        enrich_payload={"seed_id":seed.seed_id,"seed_name":seed.raw_name,"canonical_name":canonical,"aliases":aliases,"evidence":[e.to_dict() for e in bundle.evidence]}
        enrichment,enrich_response,enrich_errors=self._call(self.enrichment_prompt,enrich_payload,lambda v:self._validate_enrichment(v,allowed))
        if enrichment is None:
            return self._base(seed,"accepted",canonical,aliases,set(qualification.get("evidence_ids",[])),reason,response,"ambiguous","enrichment_validation_failed: "+" | ".join(enrich_errors),enrich_response)
        parents,parent_decisions=self._resolve_relations(seed,bundle,enrichment.get("parent_proposals",[]))
        supported=enrichment["definition_status"]=="supported"
        return ConceptResolution(
            seed_id=seed.seed_id,decision="accepted",canonical_name=canonical,
            definition=enrichment.get("definition") if supported else None,
            definition_type=enrichment.get("definition_type") if supported else None,
            definition_evidence_ids=enrichment.get("definition_evidence_ids",[]) if supported else [],
            aliases=aliases,parent_proposals=parents,parent_decisions=parent_decisions,
            evidence_ids=sorted(set(qualification.get("evidence_ids",[]))|set(enrichment.get("evidence_ids",[]))),reason=reason,
            prompt_id=self.qualification_prompt["id"],prompt_version=str(self.qualification_prompt["version"]),
            configured_model=self.models[self.model_key]["model"],served_model=(enrich_response or response or {}).get("model"),
            qualification_reason=reason,definition_status=enrichment["definition_status"],definition_reason=enrichment.get("definition_reason",""),
            enrichment_prompt_id=self.enrichment_prompt["id"],enrichment_prompt_version=str(self.enrichment_prompt["version"]))

    def _call(self,prompt:dict,payload:dict,validator:Callable):
        messages=[{"role":"system","content":prompt["system"]},{"role":"user","content":json.dumps(payload,ensure_ascii=False)}]
        errors=[]; response=None
        for attempt in range(3):
            response=self.client.complete(self.model_key,messages,response_format={"type":"json_object"},prompt_id=prompt["id"],prompt_version=str(prompt["version"]),input_unit_ids=[e.get("unit_id") for e in payload.get("evidence",[])],max_tokens=int(prompt.get("max_tokens",900)))
            try:
                value=json.loads(response["choices"][0]["message"]["content"]); validator(value)
                return value,response,errors
            except Exception as error:
                errors.append(f"{type(error).__name__}: {error}")
                if attempt<2: messages=messages[:2]+[{"role":"user","content":f"上次输出不合规：{error}。只重新输出符合约束的JSON对象。"}]
        return None,response,errors

    def _resolve_relations(self,seed,bundle,proposals):
        allowed={e.evidence_id for e in bundle.evidence}; text=normalize_name("\n".join(e.text for e in bundle.evidence)); accepted=[]; decisions=[]; isa=[]
        for p in proposals:
            name=str(p.get("name","")); ids=list(p.get("evidence_ids",[]))
            if not name or normalize_name(name) not in text or not set(ids)<=allowed:
                decisions.append(ParentDecision(name,str(p.get("relation_type") or "UNKNOWN"),"ambiguous","unknown",[],"invalid_relation_candidate",p.get("definition"))); continue
            verdict=self._classify_relation(seed,bundle,p)
            relation_type=verdict["relation_type"]
            decision=ParentDecision(name,relation_type,verdict["decision"],verdict["directness"],verdict["evidence_ids"],verdict.get("reason",""),p.get("definition")); decisions.append(decision)
            if relation_type=="IS_A" and verdict["decision"]=="accepted": isa.append((p,decision))
        if isa:
            final=self._select_direct_parents(seed,bundle,[decision for _,decision in isa])
            by_name={normalize_name(item["name"]):item for item in final}
            for proposal,decision in isa:
                verdict=by_name.get(normalize_name(decision.name),{"decision":"rejected","directness":"unknown","evidence_ids":[],"reason":"not_selected_by_global_directness"})
                decision.decision=verdict["decision"]; decision.directness=verdict["directness"]; decision.evidence_ids=verdict["evidence_ids"]; decision.reason=verdict.get("reason","")
                if decision.decision=="accepted" and decision.directness=="direct": accepted.append(ParentProposal(decision.name,"IS_A","direct",decision.evidence_ids,proposal.get("definition")))
        return accepted,decisions

    def _select_direct_parents(self,seed,bundle,candidates):
        allowed={e.evidence_id for e in bundle.evidence}
        payload={"child":seed.raw_name,"candidates":[d.to_dict() for d in candidates],"evidence":[e.to_dict() for e in bundle.evidence]}
        value,_,errors=self._call(self.directness_prompt,payload,lambda v:self._validate_directness(v,allowed,normalize_name(seed.raw_name),{normalize_name(d.name) for d in candidates}))
        verdicts=value["verdicts"] if value else [{"name":d.name,"decision":"ambiguous","directness":"unknown","evidence_ids":[],"reason":"directness_validation_failed: "+" | ".join(errors)} for d in candidates]
        evidence={e.evidence_id:e.text for e in bundle.evidence}
        for verdict in verdicts:
            text="\n".join(evidence[eid] for eid in verdict.get("evidence_ids",[]) if eid in evidence)
            if verdict.get("decision")=="accepted" and not self._has_explicit_is_a(seed.raw_name,verdict["name"],text):
                verdict["decision"]="rejected"; verdict["directness"]="unknown"
                verdict["reason"]="explicit_directional_classification_not_found"
        return verdicts

    def _classify_relation(self,seed,bundle,proposal):
        allowed={e.evidence_id for e in bundle.evidence}; selected=set(proposal.get("evidence_ids",[]))
        payload={"subject":seed.raw_name,"candidate":proposal["name"],"proposed_relation":proposal.get("relation_type"),"evidence":[e.to_dict() for e in bundle.evidence if e.evidence_id in selected]}
        value,_,errors=self._call(self.relation_prompt,payload,lambda v:self._validate_relation(v,allowed))
        return value or {"relation_type":"UNKNOWN","decision":"ambiguous","directness":"unknown","evidence_ids":[],"reason":"relation_classifier_validation_failed: "+" | ".join(errors)}

    def _base(self,seed,decision,canonical,aliases,evidence_ids,reason,response,definition_status,definition_reason=None,enrich_response=None):
        return ConceptResolution(seed_id=seed.seed_id,decision=decision,canonical_name=canonical,definition=None,definition_type=None,definition_evidence_ids=[],aliases=aliases,parent_proposals=[],parent_decisions=[],evidence_ids=sorted(evidence_ids),reason=reason,prompt_id=self.qualification_prompt["id"],prompt_version=str(self.qualification_prompt["version"]),configured_model=self.models[self.model_key]["model"],served_model=(enrich_response or response or {}).get("model"),qualification_reason=reason,definition_status=definition_status,definition_reason=definition_reason,enrichment_prompt_id=self.enrichment_prompt["id"],enrichment_prompt_version=str(self.enrichment_prompt["version"]))

    @staticmethod
    def _safe_canonical(name,seed,bundle):
        text=normalize_name("\n".join(e.text for e in bundle.evidence))
        if name and normalize_name(name) in text: return name
        # The immutable Rule object is itself a valid naming source. This also
        # covers coordinated ellipsis such as "L-MIG和L-TIG焊" -> "L-MIG焊".
        return seed.raw_name
    @staticmethod
    def _valid_aliases(aliases,bundle):
        text=normalize_name("\n".join(e.text for e in bundle.evidence)); return [a for a in aliases if a and normalize_name(a) in text]
    @staticmethod
    def _validate_qualification(v,allowed,bundle,seed):
        if v.get("decision") not in {"accepted","rejected","ambiguous"}: raise ResolutionError("invalid qualification decision")
        if not set(v.get("evidence_ids",[]))<=allowed: raise ResolutionError("qualification evidence outside bundle")
        if not isinstance(v.get("aliases",[]),list): raise ResolutionError("aliases must be a list")
        if v["decision"]=="accepted": ConceptResolver._safe_canonical(v.get("canonical_name"),seed,bundle)
    @staticmethod
    def _validate_enrichment(v,allowed):
        if v.get("definition_status") not in {"supported","insufficient_evidence","ambiguous"}: raise ResolutionError("invalid definition status")
        if not set(v.get("definition_evidence_ids",[]))<=allowed or not set(v.get("evidence_ids",[]))<=allowed: raise ResolutionError("enrichment evidence outside bundle")
        if not isinstance(v.get("parent_proposals",[]),list): raise ResolutionError("parents must be a list")
        if v["definition_status"]=="supported" and (not v.get("definition") or not v.get("definition_evidence_ids")): raise ResolutionError("supported definition needs evidence")
    @staticmethod
    def _validate_relation(v,allowed):
        allowed_types={"IS_A","HAS_SUBTYPE","SAME_AS","PART_OF","USES","PRODUCES","ATTRIBUTE_OF","TOPIC_OR_EXAMPLE","UNKNOWN"}
        if v.get("relation_type") not in allowed_types: raise ResolutionError("invalid relation type")
        if v.get("decision") not in {"accepted","rejected","ambiguous"}: raise ResolutionError("invalid relation decision")
        if v.get("directness") not in {"direct","indirect","unknown"}: raise ResolutionError("invalid parent directness")
        if not set(v.get("evidence_ids",[]))<=allowed: raise ResolutionError("parent evidence outside bundle")
    @staticmethod
    def _validate_directness(v,allowed,child_name,candidate_names):
        if not isinstance(v.get("verdicts"),list): raise ResolutionError("verdicts must be a list")
        for item in v["verdicts"]:
            if normalize_name(item.get("name","")) not in candidate_names: raise ResolutionError("unknown parent candidate")
            if item.get("decision") not in {"accepted","rejected","ambiguous"}: raise ResolutionError("invalid directness decision")
            if item.get("directness") not in {"direct","indirect","unknown"}: raise ResolutionError("invalid directness")
            if not set(item.get("evidence_ids",[]))<=allowed: raise ResolutionError("directness evidence outside bundle")
            if item.get("decision")=="accepted":
                if normalize_name(item.get("classified_child",""))!=child_name: raise ResolutionError("accepted IS_A child direction mismatch")
                if normalize_name(item.get("classified_parent",""))!=normalize_name(item.get("name","")): raise ResolutionError("accepted IS_A parent direction mismatch")
    @staticmethod
    def _has_explicit_is_a(child,parent,text):
        child=re.escape(normalize_name(child)); parent=re.escape(normalize_name(parent)); value=normalize_name(text)
        if not child or not parent or not value: return False
        forward=[rf"{child}.{{0,24}}(?:是一种|是一类|属于|归属于).{{0,12}}{parent}"]
        reverse=[rf"{parent}.{{0,24}}(?:包括|包含|分为|可分为|主要包括|主要有|称为).{{0,80}}{child}"]
        return any(re.search(pattern,value) for pattern in forward+reverse)

def load_models(path:str|Path)->dict[str,Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))["models"]
