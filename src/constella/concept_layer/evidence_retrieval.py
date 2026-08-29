from __future__ import annotations
import re
from .input_index import ConceptInputIndex, stable_hash
from .models import ConceptEvidenceBundle, EvidenceItem, ObjectSeed
from .object_seeds import normalize_name

_DEFINITION = re.compile(r"(?:是指|称为|所谓|定义为|是一种)")
_TAXONOMY = re.compile(r"(?:属于|分为|包括|分类|类型|种类|形式|主要有)")

class EvidenceRetriever:
    def __init__(self, index: ConceptInputIndex, *, neighbor_radius: int = 8, limit: int = 24) -> None:
        self.index=index; self.neighbor_radius=neighbor_radius; self.limit=limit
    def retrieve(self, seed: ObjectSeed) -> ConceptEvidenceBundle:
        methods: dict[str,set[str]]={}; scores: dict[str,int]={}
        pids={r.context_package_id for r in seed.rule_refs}
        source={u for p in pids for u in self.index.package_unit_ids(p)}
        neighbors={u for p in pids for u in self.index.section_neighbor_ids(p,self.neighbor_radius)}
        sections={tuple(self.index.package_section(p)) for p in pids}
        for uid in self.index.reading_order:
            text=self.index.unit_text(uid); score=0
            if seed.normalized_name in normalize_name(text):
                method="source_exact" if uid in source else "section_exact" if uid in neighbors else "global_exact"
                methods.setdefault(uid,set()).add(method); score+=100 if uid in source else 70 if uid in neighbors else 45
                if _DEFINITION.search(text): methods[uid].add("definition_pattern"); score+=25
                if _TAXONOMY.search(text): methods[uid].add("taxonomy_pattern"); score+=25
            if tuple(self.index.unit_section(uid)) in sections and _overlap(seed.raw_name,text)>=2:
                methods.setdefault(uid,set()).add("section_semantic"); score+=20
            if score: scores[uid]=score
        # Definitions and classification statements are frequently split over
        # adjacent graph units. Preserve those continuations as evidence, but
        # only inside the same section and only around an already matched unit.
        for uid in list(scores):
            if not ({"definition_pattern","taxonomy_pattern"} & methods[uid]): continue
            position=self.index.positions.get(uid)
            if position is None: continue
            for neighbor_position in (position-1,position+1):
                if not 0<=neighbor_position<len(self.index.reading_order): continue
                neighbor=self.index.reading_order[neighbor_position]
                if self.index.unit_section(neighbor)!=self.index.unit_section(uid): continue
                methods.setdefault(neighbor,set()).add("graph_continuation")
                scores[neighbor]=max(scores.get(neighbor,0),scores[uid]-5)
        evidence=[]
        ranked=sorted((-score,self.index.positions.get(uid,0),uid) for uid,score in scores.items())
        for _,_,uid in ranked[:self.limit]:
            evidence.append(EvidenceItem(stable_hash("evidence",seed.seed_id,uid,*sorted(methods[uid])),uid,self.index.unit_text(uid),self.index.unit_section(uid),self.index.unit_page(uid),sorted(methods[uid])))
        return ConceptEvidenceBundle(seed.seed_id,seed.raw_name,evidence,"ready" if evidence else "insufficient_evidence")

def _overlap(name:str,text:str)->int:
    key=normalize_name(name); grams={key[i:i+2] for i in range(max(0,len(key)-1))}; normalized=normalize_name(text)
    return sum(g in normalized for g in grams)
