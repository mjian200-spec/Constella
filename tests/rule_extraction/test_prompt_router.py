from __future__ import annotations

from pathlib import Path
import unittest

from constella.rule_extraction.prompt_router import (
    RoutedPromptRegistry, route_modalities, route_modalities_for_types,
)
from constella.rule_extraction.resolver import DocumentGraphIndex, iter_packages, resolve_package


ROOT = Path(__file__).resolve().parents[2]
CONTEXT = ROOT / "outputs" / "context_builder"
PROMPTS = ROOT / "prompts" / "rule_extraction"


@unittest.skipUnless((CONTEXT / "document_graph.json").is_file(), "requires real Context Builder output")
class PromptRouterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.index = DocumentGraphIndex.load(CONTEXT / "document_graph.json")
        cls.packages = {item["id"]: item for item in iter_packages(CONTEXT / "context_packages.jsonl")}

    @classmethod
    def resolved(cls, package_id: str):
        return resolve_package(cls.index, cls.packages[package_id])

    def test_routes_real_text_image_table_and_formula_packages(self) -> None:
        self.assertEqual(("text",), route_modalities(self.resolved("context_000481")))
        self.assertEqual(("text", "image"), route_modalities(self.resolved("context_000391")))
        self.assertEqual(("text", "table"), route_modalities(self.resolved("context_000801")))
        self.assertEqual(("text", "formula"), route_modalities(self.resolved("context_000540")))

    def test_summary_route_uses_the_same_unit_type_contract(self) -> None:
        self.assertEqual(("text",), route_modalities_for_types(["passage", "title"]))
        self.assertEqual(
            ("text", "image", "table", "formula"),
            route_modalities_for_types(["passage", "figure", "table", "formula"]),
        )

    def test_composes_every_specialist_for_real_mixed_package(self) -> None:
        registry = RoutedPromptRegistry(PROMPTS)
        prompt, route = registry.prompt_for(self.resolved("context_000037"))
        self.assertEqual("text+table+formula", route)
        self.assertEqual("rule_generator_routed__text__table__formula", prompt["id"])
        self.assertIn("【表格专项】", prompt["system"])
        self.assertIn("【公式专项】", prompt["system"])
        self.assertNotIn("【图像文字化专项】", prompt["system"])
        self.assertIn("【纯文本专项】", prompt["system"])

    def test_route_prompts_encode_the_key_semantic_guards(self) -> None:
        registry = RoutedPromptRegistry(PROMPTS)
        image, _ = registry.prompt_for(self.resolved("context_000391"))
        table, _ = registry.prompt_for(self.resolved("context_000801"))
        formula, _ = registry.prompt_for(self.resolved("context_000540"))
        self.assertIn("资源文字化已明确数值归属", image["system"])
        self.assertIn("表题→分组表头→行头→列头", table["system"])
        self.assertIn("普通公式的完整公式必须放在箭头方括号中", formula["system"])
        self.assertIn("资源出现在包中只表示可供核对", formula["system"])

    def test_prompt_identity_changes_with_route(self) -> None:
        registry = RoutedPromptRegistry(PROMPTS)
        text_prompt, _ = registry.prompt_for(self.resolved("context_000481"))
        formula_prompt, _ = registry.prompt_for(self.resolved("context_000540"))
        self.assertNotEqual(text_prompt["id"], formula_prompt["id"])
        self.assertNotEqual(text_prompt["version"], formula_prompt["version"])


if __name__ == "__main__":
    unittest.main()
