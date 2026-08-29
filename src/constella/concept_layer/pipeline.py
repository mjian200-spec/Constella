from __future__ import annotations
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import time,yaml
from .assembly import assemble
from .evidence_retrieval import EvidenceRetriever
from .input_index import ConceptInputIndex,fingerprint
from .io import write_json,write_jsonl
from .models import ConceptResolution
from .object_seeds import build_object_seeds
from .resolver import ConceptResolver,load_models
@dataclass(slots=True)
class ConceptLayerRuntime:
    output_dir:Path; use_llm:bool=False; model_key:str="qwen3_8_27b"; neighbor_radius:int=8; evidence_limit:int=24; max_workers:int=16; config_dir:Path|None=None; prompt_dir:Path|None=None
def load_runtime(config_dir,output_dir,use_llm=False,model_key="qwen3_8_27b"):
    d=Path(config_dir); c=yaml.safe_load((d/"pipeline.yaml").read_text()) or {}
    model_config=load_models(d/"models.yaml").get(model_key,{})
    workers=int(model_config.get("max_concurrency",c.get("max_workers",16)))
    return ConceptLayerRuntime(Path(output_dir),use_llm,model_key,int(c.get("section_neighbor_radius",8)),int(c.get("evidence_limit",24)),workers,d,Path(__file__).resolve().parents[3]/"prompts/concept_layer")
def run_concept_layer(context_output_dir,rule_output_dir,runtime,*,rule_ids=None,limit=None):
    started=time.monotonic(); runtime.output_dir.mkdir(parents=True,exist_ok=True); index=ConceptInputIndex.load(context_output_dir,rule_output_dir,rule_ids=rule_ids,limit=limit); original=fingerprint(index.rulesets)
    seeds=build_object_seeds(index); retriever=EvidenceRetriever(index,neighbor_radius=runtime.neighbor_radius,limit=runtime.evidence_limit); bundles=[retriever.retrieve(s) for s in seeds]; resolutions=[]
    if runtime.use_llm:
        resolver=ConceptResolver(load_models((runtime.config_dir or Path("configs/concept_layer"))/"models.yaml"),runtime.model_key,(runtime.prompt_dir or Path("prompts/concept_layer"))/"concept_qualification_v1.yaml")
        def resolve_one(position, seed, bundle):
            if bundle.retrieval_status!="ready":
                return position, ConceptResolution(seed_id=seed.seed_id,decision="ambiguous",canonical_name=None,definition=None,definition_type=None,definition_evidence_ids=[],aliases=[],parent_proposals=[],parent_decisions=[],evidence_ids=[],reason="insufficient_evidence")
            try: return position,resolver.resolve(seed,bundle)
            except Exception as error:
                return position,ConceptResolution(seed_id=seed.seed_id,decision="ambiguous",canonical_name=None,definition=None,definition_type=None,definition_evidence_ids=[],aliases=[],parent_proposals=[],parent_decisions=[],evidence_ids=[e.evidence_id for e in bundle.evidence],reason=f"model_call_failed: {type(error).__name__}: {error}")
        ordered=[None]*len(seeds)
        with ThreadPoolExecutor(max_workers=runtime.max_workers) as pool:
            futures=[pool.submit(resolve_one,i,s,b) for i,(s,b) in enumerate(zip(seeds,bundles,strict=True))]
            for future in as_completed(futures):
                position,resolution=future.result(); ordered[position]=resolution
        resolutions=ordered
    else:
        resolutions=[ConceptResolution(
            seed_id=s.seed_id, decision="ambiguous", canonical_name=None, definition=None,
            definition_type=None, definition_evidence_ids=[], aliases=[], parent_proposals=[],
            parent_decisions=[], evidence_ids=[e.evidence_id for e in b.evidence], reason="model_not_run")
            for s,b in zip(seeds,bundles,strict=True)]
    concepts,bindings,relations=assemble(seeds,resolutions)
    if original!=fingerprint(index.rulesets): raise RuntimeError("Concept pipeline mutated rules")
    for name,rows in (("object_seeds",seeds),("concept_evidence_bundles",bundles),("concept_resolutions",resolutions),("concepts",concepts),("rule_concept_bindings",bindings),("concept_relations",relations)): write_jsonl(runtime.output_dir/f"{name}.jsonl",(x.to_dict() for x in rows))
    report={"rule_count":sum(1 for _ in index.iter_rules()),"unique_object_seed_count":len(seeds),"evidence_ready_count":sum(b.retrieval_status=="ready" for b in bundles),"accepted_resolution_count":sum(r.decision=="accepted" for r in resolutions),"rejected_resolution_count":sum(r.decision=="rejected" for r in resolutions),"ambiguous_resolution_count":sum(r.decision=="ambiguous" for r in resolutions),"definition_supported_count":sum(r.definition_status=="supported" for r in resolutions),"definition_insufficient_count":sum(r.definition_status=="insufficient_evidence" for r in resolutions),"definition_ambiguous_count":sum(r.definition_status=="ambiguous" for r in resolutions),"parent_candidate_count":sum(len(r.parent_decisions) for r in resolutions),"parent_accepted_count":sum(d.decision=="accepted" and d.directness=="direct" for r in resolutions for d in r.parent_decisions),"parent_rejected_count":sum(d.decision=="rejected" for r in resolutions for d in r.parent_decisions),"parent_ambiguous_count":sum(d.decision=="ambiguous" for r in resolutions for d in r.parent_decisions),"parent_indirect_count":sum(d.decision=="accepted" and d.directness=="indirect" for r in resolutions for d in r.parent_decisions),"concept_count":len(concepts),"depth_0_concept_count":sum(c.origin_depth==0 for c in concepts),"depth_1_parent_count":sum(c.origin_depth==1 for c in concepts),"binding_count":len(bindings),"direct_is_a_count":len(relations),"llm_enabled":runtime.use_llm,"elapsed_seconds":round(time.monotonic()-started,3)}; write_json(runtime.output_dir/"concept_layer_report.json",report); return report
