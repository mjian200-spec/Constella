from __future__ import annotations

from pathlib import Path
import unittest

from constella.rule_extraction.image_adapter import ImageAdapter
from constella.rule_extraction.resolver import DocumentGraphIndex, iter_packages, resolve_package


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "outputs" / "context_builder"


@unittest.skipUnless((OUTPUT / "document_graph.json").is_file(), "requires real Context Builder output")
class RealContextInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.index = DocumentGraphIndex.load(OUTPUT / "document_graph.json")
        cls.packages = {item["id"]: item for item in iter_packages(OUTPUT / "context_packages.jsonl")}

    def test_real_threshold_context_resolves_all_references(self) -> None:
        package = resolve_package(self.index, self.packages["context_000465"])
        self.assertEqual(["unit_002522"], [unit.id for unit in package.core_units])
        self.assertEqual(2, len(package.constraints))
        self.assertIn("20", str(package.core_units[0].content))

    def test_real_figure_context_has_a_loadable_image(self) -> None:
        package = resolve_package(self.index, self.packages["context_000472"])
        self.assertEqual(1, len(package.assets))
        asset = package.assets[0]
        self.assertEqual("figure", asset.unit.type)
        prepared = ImageAdapter().prepare(asset.resolved_path)
        self.assertTrue(prepared.data_url.startswith("data:image/"))

    def test_real_long_table_context_is_preserved_without_resegmentation(self) -> None:
        package = resolve_package(self.index, self.packages["context_000637"])
        tables = [asset for asset in package.assets if asset.unit.type == "table"]
        self.assertEqual(1, len(tables))
        self.assertGreater(len(str(tables[0].unit.content)), 2000)
        self.assertTrue(ImageAdapter().prepare(tables[0].resolved_path).data_url.startswith("data:image/"))

    def test_real_formula_transformation_resolves_both_complete_formulas(self) -> None:
        package = resolve_package(self.index, self.packages["context_000540"])
        formulas = {asset.unit.id: str(asset.unit.content) for asset in package.assets if asset.unit.type == "formula"}
        self.assertEqual({"unit_002902", "unit_002904"}, set(formulas))
        self.assertIn(r"\tag {5-30}", formulas["unit_002902"])
        self.assertIn(r"\tag {5-31}", formulas["unit_002904"])
