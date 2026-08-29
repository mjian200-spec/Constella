from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from constella.rule_extraction.image_adapter import ImageAdapter
from constella.rule_extraction.message_builder import MultimodalMessageBuilder
from constella.rule_extraction.models import ResolvedAsset, ResolvedContextPackage, ResolvedUnit
from constella.rule_extraction.pipeline import RuleExtractionRuntime, run_rule_extraction
from constella.rule_extraction.resolver import (
    DocumentGraphIndex,
    is_rule_extraction_package,
    iter_packages,
    resolve_package,
)


def test_article_candidates_are_not_rule_extraction_packages() -> None:
    assert is_rule_extraction_package({"attributes": {"package_type": "rule"}})
    assert is_rule_extraction_package({"attributes": {"package_type": "formula_context"}})
    assert is_rule_extraction_package({"attributes": {}})
    assert not is_rule_extraction_package({"attributes": {"package_type": "article_candidate"}})
    assert is_rule_extraction_package({"attributes": {
        "package_type": "article_candidate",
        "package_role": {"status": "ok", "is_rule_package": True},
    }})
    assert not is_rule_extraction_package({"attributes": {
        "package_type": "rule",
        "package_role": {"status": "ok", "is_rule_package": False},
    }})


def test_rule_message_uses_resource_textualization_without_an_image_block() -> None:
    unit = ResolvedUnit(
        id="u1", type="figure", content="图题", source={"page": 1},
        attributes={"caption": "图题", "resource_understanding": {"description": "图的文字化结论"}},
    )
    package = ResolvedContextPackage(
        id="p1", core_units=[ResolvedUnit("u0", "passage", "正文", {"page": 1})],
        support_units=[], constraints=[], assets=[ResolvedAsset(unit, "images/a.png", None, "图题")],
        unresolved=[], section_path=[], source_package={}, source_fingerprint="x", resolver_version="2",
    )
    blocks = MultimodalMessageBuilder().context_content(package)
    assert len(blocks) == 1
    assert blocks[0]["type"] == "text"
    assert "图的文字化结论" in blocks[0]["text"]


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

    def test_context_builder_labels_package_constraints_as_candidates(self) -> None:
        package = resolve_package(self.index, self.packages["context_000465"])
        text = MultimodalMessageBuilder()._text_context(package)
        self.assertIn("包条件（已由上游确定作用域）:", text)
        self.assertIn("来源 Unit:", text)

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

    def test_dry_run_resolves_and_caches_without_creating_extraction_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            runtime = RuleExtractionRuntime(
                ROOT / "configs" / "rule_extraction", output_dir, dry_run_resolve=True,
            )
            report = run_rule_extraction(
                OUTPUT, runtime, package_ids={"context_000465", "context_000540"},
            )

            self.assertEqual(2, report["resolved_count"])
            self.assertEqual(0, report["failed_count"])
            self.assertFalse((output_dir / "rule_extraction_state.sqlite3").exists())
            self.assertTrue((output_dir / "cache" / "contexts" / "context_000540.json").is_file())
