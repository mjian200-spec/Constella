import json
from pathlib import Path
import tempfile
import unittest

from constella.context_builder.pipeline import load_runtime, run_context_builder


ROOT = Path(__file__).resolve().parents[2]


class PipelineTests(unittest.TestCase):
    def test_table_row_alignment_does_not_cross_rows(self):
        blocks = [
            {"type": "text", "text": "第5章 CO₂气体保护焊", "bbox": [0, 0, 10, 10], "page_idx": 0},
            {"type": "text", "text": "由表5-3可见，当铁锈量为0.5g时，将产生少量气孔。", "bbox": [0, 20, 10, 30], "page_idx": 0},
            {"type": "table", "table_caption": ["表5-3 试验结果"], "table_body": "<table><tr><td>铁锈量/g</td><td>结果</td></tr><tr><td>0.5</td><td>少量气孔</td></tr><tr><td>1.0</td><td>无气孔</td></tr></table>", "bbox": [0, 40, 10, 50], "page_idx": 0, "img_path": "table.png"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "input.json"; input_path.write_text(json.dumps(blocks), encoding="utf-8")
            output_dir = Path(directory) / "out"
            graph = run_context_builder(str(input_path), str(output_dir), load_runtime(ROOT / "configs/context_builder"))
            aligned = [relation.target_id for relation in graph.relations if relation.type == "ALIGNS_WITH"]
            self.assertEqual(1, len(aligned))
            self.assertTrue(aligned[0].endswith("row_01"))
            packages = [json.loads(line) for line in (output_dir / "context_packages.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([aligned[0]], packages[0]["asset_part_ids"])
            self.assertEqual(1, len(packages))
            self.assertTrue((output_dir / "document_graph.json").exists())
            self.assertTrue((output_dir / "run_report.json").exists())

    def test_whole_figure_is_retained_without_curve_parts(self):
        blocks = [
            {"type": "text", "text": "图5-18中的曲线3如图所示。", "bbox": [0, 0, 10, 10], "page_idx": 0},
            {"type": "image", "image_caption": ["图5-18 压力关系"], "img_path": "figure.jpg", "bbox": [0, 20, 10, 30], "page_idx": 0},
        ]
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "input.json"; input_path.write_text(json.dumps(blocks), encoding="utf-8")
            graph = run_context_builder(str(input_path), str(Path(directory) / "out"), load_runtime(ROOT / "configs/context_builder"))
            self.assertEqual([], [unit for unit in graph.units.values() if unit.type == "figure_curve"])
            self.assertTrue(any(item.type == "figure_substructure_unavailable" for item in graph.ambiguities.values()))

    def test_repeated_table_header_is_split_into_logical_rows(self):
        blocks = [
            {"type": "text", "text": "由表5-3可见，在铁锈量为0.5g时将产生气孔。当铁锈量为1.0g时将产生气孔。", "bbox": [0, 0, 10, 10], "page_idx": 0},
            {"type": "table", "table_caption": ["表5-3 试验结果"], "table_body": "<table><tr><td>铁锈/g</td><td>结果</td><td>铁锈/g</td><td>结果</td></tr><tr><td>0.3</td><td>无</td><td>1.0</td><td>有</td></tr><tr><td>0.5</td><td>有</td><td>1.2</td><td>有</td></tr></table>", "bbox": [0, 20, 10, 30], "page_idx": 0},
        ]
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "input.json"; input_path.write_text(json.dumps(blocks), encoding="utf-8")
            output_dir = Path(directory) / "out"
            graph = run_context_builder(str(input_path), str(output_dir), load_runtime(ROOT / "configs/context_builder"))
            packages = [json.loads(line) for line in (output_dir / "context_packages.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(2, len(packages))
            self.assertNotEqual(packages[0]["asset_part_ids"], packages[1]["asset_part_ids"])
            self.assertEqual({"0.5", "1"}, {package["attributes"]["matched_values"][0] for package in packages})

    def test_formula_reference_aligns_with_formula(self):
        blocks = [
            {"type": "text", "text": "可根据式（5-21）计算焊丝成分。", "bbox": [0, 0, 10, 10], "page_idx": 0},
            {"type": "equation", "text": "Cw = me * Ce", "bbox": [0, 20, 10, 30], "page_idx": 0},
        ]
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "input.json"; input_path.write_text(json.dumps(blocks), encoding="utf-8")
            graph = run_context_builder(str(input_path), str(Path(directory) / "out"), load_runtime(ROOT / "configs/context_builder"))
            self.assertTrue(any(relation.type == "ALIGNS_WITH" and relation.source_id == "unit_000000" for relation in graph.relations))
