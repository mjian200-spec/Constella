from __future__ import annotations
import uuid
from collections import defaultdict
from .input_index import stable_hash
from .models import Concept,ConceptRelation,ConceptResolution,ObjectSeed,RuleConceptBinding
from .object_seeds import normalize_name

_NS=uuid.UUID("086b8926-e737-4dce-9705-4ae0a62b71e8")
def _id(name): return str(uuid.uuid5(_NS,"constella:concept:"+normalize_name(name)))

class _UnionFind:
    def __init__(self,keys): self.parent={k:k for k in keys}
    def find(self,key):
        while self.parent[key]!=key:
            self.parent[key]=self.parent[self.parent[key]]; key=self.parent[key]
        return key
    def union(self,left,right):
        a,b=self.find(left),self.find(right)
        if a!=b: self.parent[max(a,b)]=min(a,b)

def assemble(seeds:list[ObjectSeed],resolutions:list[ConceptResolution]):
    seed_map={s.seed_id:s for s in seeds}
    accepted=[r for r in resolutions if r.decision=="accepted" and r.canonical_name]
    base=defaultdict(list)
    for r in accepted: base[normalize_name(r.canonical_name)].append(r)
    names={normalize_name(r.canonical_name):normalize_name(r.canonical_name) for r in accepted}
    for r in accepted:
        key=normalize_name(r.canonical_name)
        for alias in r.aliases: names.setdefault(normalize_name(alias),key)
    union=_UnionFind(base)
    external_aliases=defaultdict(set); preferred_names=defaultdict(list)
    for r in accepted:
        child=normalize_name(r.canonical_name)
        for decision in r.parent_decisions:
            if decision.decision!="accepted" or decision.relation_type!="SAME_AS": continue
            target=normalize_name(decision.name)
            if target in names: union.union(child,names[target])
            elif target: external_aliases[child].add(decision.name)
            if target: preferred_names[child].append(decision.name)
    groups=defaultdict(list)
    for key,items in base.items(): groups[union.find(key)].extend(items)
    concepts={}; resolution_concept={}; bindings=[]
    for root,items in groups.items():
        preferred=[name for key,values in preferred_names.items() if union.find(key)==root for name in values]
        existing={normalize_name(r.canonical_name):r.canonical_name for r in items}
        name=next((existing[normalize_name(p)] for p in preferred if normalize_name(p) in existing),preferred[0] if preferred else items[0].canonical_name); cid=_id(name)
        aliases={a for r in items for a in r.aliases}
        aliases.update(r.canonical_name for r in items)
        for key,values in external_aliases.items():
            if union.find(key)==root: aliases.update(values)
        aliases={a for a in aliases if normalize_name(a)!=normalize_name(name)}
        concept=Concept(cid,name,next((r.definition for r in items if r.definition),None),next((r.definition_type for r in items if r.definition),None),sorted(aliases),[r.seed_id for r in items],sorted({e for r in items for e in r.evidence_ids}),0)
        concepts[root]=concept
        for r in items:
            resolution_concept[r.seed_id]=concept
            for ref in seed_map[r.seed_id].rule_refs:
                bindings.append(RuleConceptBinding(stable_hash("binding",ref.rule_id,ref.state_expression_id,cid),ref.rule_id,ref.state_expression_id,ref.side,ref.position,"object",ref.raw_object,cid,r.evidence_ids))

    name_to_concept={}
    for root,concept in concepts.items():
        name_to_concept[normalize_name(concept.canonical_name)]=concept
        for alias in concept.aliases: name_to_concept[normalize_name(alias)]=concept
    edge_evidence=defaultdict(set)
    for r in accepted:
        child=resolution_concept[r.seed_id]
        for p in r.parent_proposals:
            if p.relation_type!="IS_A" or p.directness!="direct": continue
            parent=name_to_concept.get(normalize_name(p.name))
            if parent is None:
                parent=Concept(_id(p.name),p.name,p.definition,"induced" if p.definition else None,[],[],p.evidence_ids,1)
                concepts[normalize_name(p.name)]=parent; name_to_concept[normalize_name(p.name)]=parent
            if child.concept_id!=parent.concept_id: edge_evidence[(child.concept_id,parent.concept_id)].update(p.evidence_ids)

    # First prevent cycles, then remove every edge whose target is reachable
    # through another parent. The remaining graph contains direct IS_A only.
    kept=set()
    for edge in sorted(edge_evidence):
        child,parent=edge
        if not _reachable(parent,child,kept): kept.add(edge)
    direct=set(kept)
    for edge in kept:
        if _reachable(edge[0],edge[1],kept-{edge}): direct.discard(edge)
    relations=[ConceptRelation(stable_hash("relation",child,parent),child,"IS_A",parent,"direct",sorted(edge_evidence[(child,parent)])) for child,parent in sorted(direct)]
    return list({c.concept_id:c for c in concepts.values()}.values()),bindings,relations

def _reachable(start,target,edges):
    graph=defaultdict(set)
    for child,parent in edges: graph[child].add(parent)
    stack=[start]; seen=set()
    while stack:
        node=stack.pop()
        if node==target: return True
        if node not in seen: seen.add(node); stack.extend(graph[node]-seen)
    return False
