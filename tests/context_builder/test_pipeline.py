from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from constella.context_builder.pipeline import load_runtime, run_context_builder


ROOT = Path(__file__).resolve().parents[2]
INPUT_PATH = ROOT / "GMAW/hybrid_ocr/GMAW(OCR)_content_list.json"


@unittest.skipUnless(INPUT_PATH.exists(), "requires the local ignored GMAW MinerU input")
class RealGMAWPipelineTests(unittest.TestCase):
    """Every assertion uses blocks and expected evidence from the real OCR input."""

    @classmethod
    def setUpClass(cls):
        cls.raw_blocks = json.loads(INPUT_PATH.read_text(encoding="utf-8"))
        cls.directory = tempfile.TemporaryDirectory()
        cls.output_dir = Path(cls.directory.name) / "context_builder"
        cls.graph = run_context_builder(
            str(INPUT_PATH), str(cls.output_dir), load_runtime(ROOT / "configs/context_builder")
        )
        cls.packages = [
            json.loads(line) for line in (cls.output_dir / "context_packages.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        cls.structured_candidates = [
            json.loads(line) for line in (cls.output_dir / "ontology_candidates.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        cls.positions = {
            unit_id: index for index, unit_id in enumerate(cls.graph.metadata["reading_order"])
            if unit_id in cls.graph.units
        }

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def raw(self, index: int) -> dict:
        return self.raw_blocks[index]

    def unit(self, index: int):
        unit_id = f"unit_{index:06d}"
        self.assertIn(unit_id, self.graph.units, f"raw block {index} was unexpectedly removed")
        unit = self.graph.units[unit_id]
        self.assertEqual(f"gmaw_ocr__content_list:{index}", unit.source.original_block_id)
        return unit

    def test_real_output_contract(self):
        self.assertEqual(len(self.raw_blocks) - 131, len(self.graph.units))
        self.assertEqual(sum("rule" in unit.role for unit in self.graph.units.values()), len(self.packages))
        self.assertFalse(any(unit.type.startswith("table_") for unit in self.graph.units.values()))
        self.assertFalse(any(unit.type == "formula_variable" for unit in self.graph.units.values()))
        self.assertTrue(all(item.type == "condition_conflict" for item in self.graph.ambiguities.values()))
        self.assertEqual(
            {"document_graph.json", "context_packages.jsonl", "ontology_candidates.jsonl", "ambiguities.jsonl", "run_report.json"},
            {path.name for path in self.output_dir.iterdir()},
        )

    def test_toc_and_front_matter_use_real_reset_evidence(self):
        self.assertEqual("目录", self.raw(121)["text"])
        self.assertEqual("第1章绪论", self.raw(131)["text"])
        self.assertEqual("unit_000121", self.graph.metadata["front_matter"]["toc_unit_id"])
        self.assertEqual("unit_000131", self.graph.metadata["front_matter"]["body_start_unit_id"])
        self.assertFalse(any(int(unit_id.rsplit("_", 1)[1]) < 131 for unit_id in self.graph.units))

    def test_real_heading_evidence_rejects_headers_asides_and_ordinal_runs(self):
        for index, heading in (
            (132, "1.1 气体保护焊的发展和历史"),
            (140, "1.2 气体保护焊的分类"),
            (488, "2.2 电弧的能量转换"),
            (1510, "3.3 母材熔化与焊缝成形"),
            (3025, "5.4 $\\mathrm{CO}_{2}$ 焊的焊接工艺"),
        ):
            self.assertEqual(heading, self.raw(index)["text"].strip())
            self.assertEqual("title", self.unit(index).type)
        self.assertEqual("1）气体保护焊效率高", self.raw(157)["text"])
        self.assertEqual("passage", self.unit(157).type)
        self.assertEqual("header", self.raw(152)["type"])
        self.assertEqual("passage", self.unit(152).type)
        self.assertEqual("aside_text", self.raw(2559)["type"])
        self.assertEqual("passage", self.unit(2559).type)

    def test_real_table_formula_and_caption_assets_remain_whole_and_linked(self):
        self.assertEqual("table", self.raw(2497)["type"])
        self.assertEqual("table", self.unit(2497).type)
        self.assertIn("式（5-21）", self.raw(2488)["text"])
        self.assertEqual("equation", self.raw(2491)["type"])
        self.assertEqual("formula", self.unit(2491).type)
        relation_types = {
            relation.type for relation in self.graph.relations
            if relation.source_id == "unit_002488" and relation.target_id == "unit_002491"
        }
        self.assertEqual({"MENTIONS", "ALIGNS_WITH"}, relation_types)
        self.assertEqual("5. 细熔滴的冲击力", self.raw(641)["text"])
        self.assertEqual("图2-31 熔滴的冲击力", self.raw(644)["image_caption"][0])
        self.assertTrue(any(
            relation.source_id == "unit_000641" and relation.target_id == "unit_000644"
            and relation.evidence == ["asset_reference.caption_description"]
            for relation in self.graph.relations
        ))

    def test_real_relative_reference_and_direct_modal_result(self):
        self.assertIn("下表", self.raw(718)["text"])
        self.assertTrue(any(
            relation.source_id == "unit_000718" and relation.target_id == "unit_000734"
            and relation.evidence == ["asset_reference.relative"]
            for relation in self.graph.relations
        ))
        self.assertIn("否则将产生气孔", self.raw(2522)["text"])
        unit = self.unit(2522)
        self.assertIn("rule", unit.role)
        self.assertIn("rule.causal_result", unit.attributes["matched_pattern_ids"])

    def test_real_structured_candidate_output_uses_classification_source(self):
        self.assertIn("可分为钨极气体保护焊", self.raw(141)["text"])
        unit = self.unit(141)
        self.assertIn("structured_candidate", unit.role)
        self.assertIn("structured.classification", unit.attributes["matched_pattern_ids"])
        record = next(item for item in self.structured_candidates if item["unit_id"] == "unit_000141")
        self.assertEqual("gmaw_ocr__content_list:141", record["source"]["original_block_id"])

    def test_real_material_condition_is_terminated_by_the_next_material_condition(self):
        self.assertIn("使用铜、钢、铝、镁等材料做电极", self.raw(381)["text"])
        constraints = [item for item in self.graph.constraints.values() if item.source_id == "unit_000381" and item.type == "材料"]
        self.assertEqual(2, len(constraints))
        first, second = constraints
        self.assertEqual("unit_000381", first.scope["end_unit_id"])
        self.assertEqual("unit_000461", second.scope["end_unit_id"])

    def test_real_scopes_and_packages_exclude_core_conditions(self):
        self.assertIn("在焊接镇静低碳钢和低合金钢时", self.raw(2522)["text"])
        package = next(item for item in self.packages if "unit_002522" in item["core_unit_ids"])
        package_sources = {self.graph.constraints[constraint_id].source_id for constraint_id in package["constraint_ids"]}
        self.assertNotIn("unit_002522", package_sources)
        for package in self.packages:
            core_position = self.positions[package["core_unit_ids"][0]]
            for constraint_id in package["constraint_ids"]:
                source_id = self.graph.constraints[constraint_id].source_id
                source = self.graph.units[source_id]
                self.assertTrue(
                    source.type == "title" or self.positions[source_id] < core_position,
                    f"{package['id']} contains a condition from its own core passage",
                )
