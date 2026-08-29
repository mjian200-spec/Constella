from __future__ import annotations
import json
import tempfile
import unittest
import yaml
from pathlib import Path
from constella.concept_layer.assembly import assemble
from constella.concept_layer.input_index import ConceptInputIndex, _load_rulesets
from constella.concept_layer.models import ConceptEvidenceBundle,ConceptResolution,EvidenceItem,ObjectSeed,ParentDecision,ParentProposal
from constella.concept_layer.object_seeds import build_object_seeds
from constella.concept_layer.resolver import ConceptResolver

class FakeClient:
    def __init__(self,responses): self.responses=responses; self.calls=[]
    def complete(self,model_key,messages,**kwargs):
        prompt_id=kwargs["prompt_id"]; self.calls.append(prompt_id)
        value=self.responses[prompt_id]
        return {"model":"fake","choices":[{"message":{"content":__import__('json').dumps(value,ensure_ascii=False)}}]}

class NewConceptPipelineTests(unittest.TestCase):
    def index(self):
        return ConceptInputIndex({"units":{"u":{"content":"元素的电离电压","attributes":{},"source":{}}},"metadata":{"reading_order":["u"]}},[{"id":"p","core_unit_ids":["u"],"attributes":{}}],[{"rules":[{"id":"r1","context_package_id":"p","conditions":[],"antecedents":[{"id":"s1","object":"元素","raw_state":"H"}],"consequents":[{"id":"s2","object":"电离电压","raw_state":"13.5"}]},{"id":"r2","context_package_id":"p","conditions":[],"antecedents":[{"id":"s3","object":"元素","raw_state":"He"}],"consequents":[]}]}])
    def test_only_objects_become_seeds_and_identical_objects_are_grouped(self):
        seeds=build_object_seeds(self.index()); names={s.raw_name:s for s in seeds}
        self.assertEqual({"元素","电离电压"},set(names)); self.assertEqual(2,len(names["元素"].rule_refs)); self.assertNotIn("H",names); self.assertNotIn("13.5",names)
    def test_project_real_validation_defaults_to_100_rules_and_16_workers(self):
        config=yaml.safe_load(Path("configs/concept_layer/evaluation.yaml").read_text(encoding="utf-8"))
        self.assertEqual(100,config["real_rule_count"]); self.assertEqual(16,config["max_workers"])
        self.assertTrue(config["use_llm"]); self.assertEqual("qwen3_8_27b",config["model_key"])
    def test_flat_current_run_export_takes_precedence_over_stale_ruleset_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/"rulesets").mkdir()
            (root/"rulesets"/"old.json").write_text(json.dumps({"context_package_id":"old","rules":[{"id":"old_rule"}]}),encoding="utf-8")
            (root/"structured_rules.jsonl").write_text(json.dumps({"id":"new_rule","context_package_id":"new"})+"\n",encoding="utf-8")
            loaded=_load_rulesets(root)
        self.assertEqual(["new_rule"],[rule["id"] for ruleset in loaded for rule in ruleset["rules"]])
    def test_resolution_assembles_binding_and_one_direct_parent(self):
        seeds=build_object_seeds(self.index()); element=next(s for s in seeds if s.raw_name=="元素")
        resolution=ConceptResolution(
            seed_id=element.seed_id, decision="accepted", canonical_name="元素",
            definition="构成物质的基本类别", definition_type="induced",
            definition_evidence_ids=["e1"], aliases=[],
            parent_proposals=[ParentProposal("物质类别","IS_A","direct",["e1"])],
            parent_decisions=[], evidence_ids=["e1"], reason="accepted")
        concepts,bindings,relations=assemble(seeds,[resolution])
        self.assertEqual(2,len(concepts)); self.assertEqual(2,len(bindings)); self.assertEqual(1,len(relations)); self.assertEqual({0,1},{c.origin_depth for c in concepts})
    def test_insufficient_definition_does_not_revoke_qualified_concept(self):
        seed=next(s for s in build_object_seeds(self.index()) if s.raw_name=="元素")
        bundle=ConceptEvidenceBundle(seed.seed_id,"元素",[EvidenceItem("e1","u","元素的电离电压",[],None,["source_exact"])],"ready")
        client=FakeClient({
            "concept_qualification":{"decision":"accepted","canonical_name":"元素","aliases":[],"evidence_ids":["e1"],"reason":"stable term"},
            "concept_enrichment":{"definition_status":"insufficient_evidence","definition":None,"definition_type":None,"definition_evidence_ids":[],"definition_reason":"mention only","parent_proposals":[],"evidence_ids":["e1"]},
        })
        resolver=ConceptResolver({"m":{"model":"fake"}},"m","prompts/concept_layer/concept_qualification_v1.yaml",client)
        result=resolver.resolve(seed,bundle)
        self.assertEqual("accepted",result.decision); self.assertIsNone(result.definition)
        self.assertEqual("insufficient_evidence",result.definition_status)
        self.assertEqual(["concept_qualification","concept_enrichment"],client.calls)
    def test_rule_object_remains_canonical_for_coordinated_ellipsis(self):
        seed=next(s for s in build_object_seeds(self.index()) if s.raw_name=="元素")
        bundle=ConceptEvidenceBundle(seed.seed_id,"元素",[EvidenceItem("e1","u","H和He元素",[],None,["source_exact"])],"ready")
        client=FakeClient({
            "concept_qualification":{"decision":"accepted","canonical_name":"化学元素","aliases":[],"evidence_ids":["e1"],"reason":"stable"},
            "concept_enrichment":{"definition_status":"insufficient_evidence","definition":None,"definition_type":None,"definition_evidence_ids":[],"definition_reason":"insufficient","parent_proposals":[],"evidence_ids":["e1"]},
        })
        result=ConceptResolver({"m":{"model":"fake"}},"m","prompts/concept_layer/concept_qualification_v1.yaml",client).resolve(seed,bundle)
        self.assertEqual("accepted",result.decision); self.assertEqual("元素",result.canonical_name)
    def test_same_as_merges_concepts_instead_of_creating_hierarchy(self):
        seeds=build_object_seeds(self.index()); element=next(s for s in seeds if s.raw_name=="元素"); voltage=next(s for s in seeds if s.raw_name=="电离电压")
        left=self.resolution(element,"元素",decisions=[ParentDecision("电离电压","SAME_AS","accepted","unknown",["e1"],"equivalent")])
        right=self.resolution(voltage,"电离电压")
        concepts,bindings,relations=assemble(seeds,[left,right])
        self.assertEqual(1,len(concepts)); self.assertEqual(3,len(bindings)); self.assertEqual([],relations)
        self.assertEqual("电离电压",concepts[0].canonical_name); self.assertIn("元素",concepts[0].aliases)
    def test_global_directness_removes_ancestor_edge(self):
        seeds=[ObjectSeed(f"s{x}",x,x.lower(),[]) for x in ("A","B","C")]
        rows=[self.resolution(seeds[0],"A",parents=[ParentProposal("B","IS_A","direct",["ab"]),ParentProposal("C","IS_A","direct",["ac"])]),self.resolution(seeds[1],"B",parents=[ParentProposal("C","IS_A","direct",["bc"])]),self.resolution(seeds[2],"C")]
        concepts,_,relations=assemble(seeds,rows); names={c.concept_id:c.canonical_name for c in concepts}
        edges={(names[r.child_concept_id],names[r.parent_concept_id]) for r in relations}
        self.assertEqual({("A","B"),("B","C")},edges)
    def test_explicit_is_a_gate_rejects_name_containment_and_accepts_classification(self):
        self.assertFalse(ConceptResolver._has_explicit_is_a("逆变式CO2焊机","CO2焊机","逆变式CO2焊机问世"))
        self.assertTrue(ConceptResolver._has_explicit_is_a("脉冲TIG焊","TIG焊","脉冲TIG焊是一种TIG焊方法"))
        self.assertTrue(ConceptResolver._has_explicit_is_a("离子","气体粒子","气体粒子包括中性粒子、电子和离子"))
    @staticmethod
    def resolution(seed,name,parents=None,decisions=None):
        return ConceptResolution(seed_id=seed.seed_id,decision="accepted",canonical_name=name,definition=None,definition_type=None,definition_evidence_ids=[],aliases=[],parent_proposals=parents or [],parent_decisions=decisions or [],evidence_ids=[],reason="accepted")

if __name__=="__main__": unittest.main()
