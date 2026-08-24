from __future__ import annotations

import unittest
import re
from pathlib import Path

from constella.rule_extraction.parser import parse_final_expression
from constella.rule_extraction.generator import load_prompt
from constella.rule_extraction.pipeline import _load_model_output, _store_model_output
from constella.rule_extraction.reflection_patch import ReflectionPatchError, addressed_draft, apply_reflection_patch


class RealModelOutputParserTests(unittest.TestCase):
    def test_formula_inside_arrow_can_contain_square_brackets(self) -> None:
        expression = """规则组1
C: 电阻R|忽略
R: 焊接回路|燃弧 —[U=[R+B]I]→ 电压平衡关系|成立"""
        ruleset = parse_final_expression(
            expression, "context_formula_arrow", prompt_id="rule_generator", prompt_version="11", model="qwen",
        )
        self.assertEqual("U=[R+B]I", ruleset.rules[0].relation)

    def test_few_shot_examples_are_injected_into_the_actual_model_system_prompt(self) -> None:
        prompt = load_prompt(Path(__file__).parents[2] / "prompts" / "rule_extraction" / "rule_reflector_full_v1.yaml")
        self.assertEqual(2, prompt["version"])
        self.assertIn("少样本格式", prompt["system"])
        self.assertIn("完整语义稀疏补丁审核器", prompt["system"])
        self.assertIn("真实上下文包 context_000255", prompt["system"])
        self.assertIn("原始生成初稿", prompt["system"])
        self.assertIn("焊接回路|燃弧", prompt["system"])

    def test_all_prompt_stages_include_semantic_failure_controls_and_real_examples(self) -> None:
        prompt_dir = Path(__file__).parents[2] / "prompts" / "rule_extraction"
        generator = load_prompt(prompt_dir / "rule_generator_v1.yaml")
        reflector = load_prompt(prompt_dir / "rule_reflector_full_v1.yaml")

        self.assertEqual(11, generator["version"])
        self.assertEqual(2, reflector["version"])
        self.assertIn("原始图片用于核对字形", generator["system"])
        self.assertIn("绝不猜测缺失词", generator["system"])
        self.assertIn("母材材质|低碳钢", generator["system"])
        self.assertIn("约束候选→作用域判定→C/R归位", generator["system"])
        self.assertIn("包约束即使标为certain", generator["system"])
        self.assertIn("只有“对象和状态都相同”", generator["system"])
        self.assertIn("约束分析：标题和临近正文共同限定", generator["system"])
        self.assertIn("没有约束时也必须写“C: 无”", generator["system"])
        self.assertIn("复杂截面药芯焊丝|挺度 —[优于]→ O形药芯焊丝|挺度", generator["system"])
        self.assertIn("NO_CHANGES不是预设结论", reflector["system"])
        self.assertIn("只改措辞、同义词、语序、标点", reflector["system"])
        self.assertIn("完整语义稀疏补丁审核器", reflector["system"])
        self.assertIn("正确内容不重新输出", reflector["system"])
        self.assertIn("DELETE_R G/R", reflector["system"])
        self.assertIn("真实上下文包 context_000540", reflector["system"])
        self.assertIn("真实上下文包 context_000255", reflector["system"])
        self.assertIn("完整公式放在箭头中", reflector["system"])
        self.assertIn("对象|适用状态 —[完整公式]→ 目标量或关系|含义", generator["system"])
        self.assertIn("式号|完整原公式 —[等价于/可化简为/变换为]→ 式号|完整结果公式", generator["system"])
        self.assertIn("焊接回路|燃弧 —[L \\times \\frac{\\mathrm{d}i}{\\mathrm{d}t}=E-U_a]→ 电压平衡关系|成立", generator["system"])
        self.assertIn("式(5-30)|Uo/Ug=", reflector["system"])
        self.assertNotIn("式(5-30)|原公式 —[可简化为]→ 式(5-31)|简化公式", reflector["system"])

    def test_every_real_few_shot_reference_output_is_parseable(self) -> None:
        prompt_dir = Path(__file__).parents[2] / "prompts" / "rule_extraction"
        for prompt_path in sorted(prompt_dir.glob("*.yaml")):
            prompt = load_prompt(prompt_path)
            if prompt["id"].startswith("rule_reflector"):
                continue
            for index, example in enumerate(prompt.get("examples", []), start=1):
                ruleset = parse_final_expression(
                    example["output"],
                    f"{prompt['id']}_example_{index}",
                    prompt_id=prompt["id"],
                    prompt_version=str(prompt["version"]),
                    model="few-shot-reference",
                )
                has_rule_line = any(line.lstrip().startswith("R:") for line in example["output"].splitlines())
                self.assertTrue(has_rule_line)
                self.assertGreater(
                    len(ruleset.rules),
                    0,
                    f"{prompt_path.name} example {index} produced no parsed rules",
                )

    def test_examples_cover_constraint_sources_and_diverse_rule_types(self) -> None:
        prompt_dir = Path(__file__).parents[2] / "prompts" / "rule_extraction"
        generator = load_prompt(prompt_dir / "rule_generator_v1.yaml")
        reflector = load_prompt(prompt_dir / "rule_reflector_full_v1.yaml")

        generator_system = generator["system"]
        for source in ("标题路径：", "临近正文：", "包约束候选：", "核心正文：", "表6-43列头："):
            self.assertIn(source, generator_system)
        self.assertIn("C: 环境温度|室温", generator_system)
        self.assertNotIn("C: 温度|30℃以上", generator_system)
        self.assertIn("C: 焊接方法|MIG/MAG焊 + 缺陷种类|气孔", generator_system)
        self.assertIn("C: 母材材质|不锈钢", generator_system)
        self.assertIn("R: 焊接层次|多层焊 —[应采用]→", generator_system)
        self.assertNotIn("C: 母材材质|不锈钢 + 焊接层次|多层焊", generator_system)

        relations = set()
        for index, example in enumerate(generator["examples"], start=1):
            ruleset = parse_final_expression(
                example["output"], f"generator_diversity_{index}",
                prompt_id=generator["id"], prompt_version=str(generator["version"]), model="few-shot-reference",
            )
            relations.update(rule.relation for rule in ruleset.rules)
        self.assertTrue({"禁止", "解决措施", "应选择", "应采用", "增加", "减少", "可化简为"}.issubset(relations))

        self.assertFalse(any(example["output"].strip() == "无规则" for example in generator["examples"]))
        self.assertTrue(any(example["output"].strip().startswith("REPLACE_R") for example in reflector["examples"]))
        all_reference_outputs = "\n".join(
            example["output"]
            for prompt in (generator, reflector)
            for example in prompt["examples"]
        )
        for reversed_attribute in ("|母材材质", "|保护气体成分", "气孔|缺陷种类", "裂纹|缺陷种类"):
            self.assertNotIn(reversed_attribute, all_reference_outputs)

    def test_every_reflection_patch_applies_and_produces_parseable_dsl(self) -> None:
        prompt = load_prompt(Path(__file__).parents[2] / "prompts" / "rule_extraction" / "rule_reflector_full_v1.yaml")
        for index, example in enumerate(prompt["examples"], start=1):
            draft = example["input"].split("原始生成初稿：\n", 1)[1]
            draft = re.sub(r"(?m)^\s*\[[^]]+\]\s*", "", draft)
            candidate = apply_reflection_patch(draft, example["output"])
            ruleset = parse_final_expression(
                candidate, f"reflection_patch_{index}", prompt_id=prompt["id"],
                prompt_version=str(prompt["version"]), model="few-shot-reference",
            )
            self.assertGreater(len(ruleset.rules), 0)

    def test_full_reflector_examples_apply(self) -> None:
        prompt_dir = Path(__file__).parents[2] / "prompts" / "rule_extraction"
        prompt = load_prompt(prompt_dir / "rule_reflector_full_v1.yaml")
        self.assertEqual("rule_reflector_full", prompt["id"])
        for index, example in enumerate(prompt["examples"], start=1):
            draft = example["input"].split("原始生成初稿：\n", 1)[1]
            draft = re.sub(r"(?m)^\s*\[[^]]+\]\s*", "", draft)
            candidate = apply_reflection_patch(draft, example["output"])
            ruleset = parse_final_expression(
                candidate, f"full_reflection_patch_{index}", prompt_id=prompt["id"],
                prompt_version=str(prompt["version"]), model="few-shot-reference",
            )
            self.assertGreater(len(ruleset.rules), 0)

    def test_sparse_patch_preserves_untouched_rules_verbatim(self) -> None:
        draft = """规则组1
C: 外部干扰|无
R: 控制法|采用 —[实现]→ 焊接过程|良好
规则组2
C: 外部干扰|无
R: 弧长|改变 —[导致]→ 控制法|不能自动调节弧长"""
        candidate = apply_reflection_patch(draft, "SET_C 2\nC: 外部干扰|存在")
        self.assertIn("C: 外部干扰|无\nR: 控制法|采用 —[实现]→ 焊接过程|良好", candidate)
        self.assertIn("C: 外部干扰|存在\nR: 弧长|改变 —[导致]→ 控制法|不能自动调节弧长", candidate)
        addressed = addressed_draft(draft)
        self.assertIn("[1/1] R: 控制法|采用", addressed)
        self.assertIn("[2/1] R: 弧长|改变", addressed)

    def test_sparse_patch_rejects_unknown_or_conflicting_addresses(self) -> None:
        draft = "规则组1\nR: A|1 —[导致]→ B|2"
        with self.assertRaises(ReflectionPatchError):
            apply_reflection_patch(draft, "DELETE_R 1/2")
        with self.assertRaises(ReflectionPatchError):
            apply_reflection_patch(draft, "REPLACE_R 1/1\nR: A|2 —[导致]→ B|2\nDELETE_R 1/1")

    def test_replace_all_recovers_a_noncanonical_draft(self) -> None:
        replacement = "规则组1\nR: A|1 —[导致]→ B|2"
        self.assertEqual(
            "规则组1\nC: 无\nR: A|1 —[导致]→ B|2",
            apply_reflection_patch("这不是可编辑DSL", f"REPLACE_ALL\n{replacement}\nEND_ALL"),
        )

    def test_reflection_canonicalizes_missing_constraint_before_rules(self) -> None:
        draft = "规则组1\nR: A|1 —[导致]→ B|2\nR: B|2 —[导致]→ C|3"
        self.assertEqual(
            "规则组1\nC: 无\nR: A|1 —[导致]→ B|2\nR: B|2 —[导致]→ C|3",
            apply_reflection_patch(draft, "NO_CHANGES"),
        )

    def test_delete_constraint_keeps_explicit_none_placeholder(self) -> None:
        draft = "规则组1\nC: 环境|室温\nR: A|1 —[导致]→ B|2"
        self.assertEqual(
            "规则组1\nC: 无\nR: A|1 —[导致]→ B|2",
            apply_reflection_patch(draft, "DELETE_C 1"),
        )

    def test_each_constraint_applies_only_to_following_rules_in_its_group(self) -> None:
        expression = """规则组1
C: 无
R: A|1 —[导致]→ B|2
R: B|2 —[导致]→ C|3
规则组2
C: 环境|室温
R: D|4 —[导致]→ E|5"""
        ruleset = parse_final_expression(
            expression, "context_group_scope", prompt_id="rule_generator", prompt_version="10", model="qwen",
        )
        self.assertEqual([], ruleset.rules[0].conditions)
        self.assertEqual([], ruleset.rules[1].conditions)
        self.assertEqual("室温", ruleset.rules[2].conditions[0].raw_state)

    def test_reflection_rejects_a_second_constraint_inserted_after_rules(self) -> None:
        draft = "规则组1\nC: 无\nR: A|1 —[导致]→ B|2\nC: 环境|室温\nR: D|4 —[导致]→ E|5"
        with self.assertRaises(ReflectionPatchError):
            apply_reflection_patch(draft, "NO_CHANGES")

    def test_deleted_group_number_can_be_reused_by_a_later_add_group(self) -> None:
        draft = """规则组1
C: 无
R: A|1 —[导致]→ B|2
规则组2
C: 旧约束|存在
R: C|3 —[导致]→ D|4"""
        patch = """DELETE_GROUP 2
ADD_GROUP
规则组2
C: 新约束|存在
R: E|5 —[导致]→ F|6
END_GROUP"""
        self.assertEqual(
            "规则组1\nC: 无\nR: A|1 —[导致]→ B|2\n规则组2\nC: 新约束|存在\nR: E|5 —[导致]→ F|6",
            apply_reflection_patch(draft, patch),
        )

    def test_identical_sparse_edits_are_ignored_as_no_ops(self) -> None:
        draft = "规则组1\nC: 环境|室温\nR: A|1 —[导致]→ B|2"
        patch = """SET_C 1
C: 环境|室温
REPLACE_R 1/1
R: A|1 —[导致]→ B|2"""
        self.assertEqual(draft, apply_reflection_patch(draft, patch))

    def test_model_output_cache_is_invalidated_by_prompt_version(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            output_dir = Path(directory)
            _store_model_output(output_dir, "context_test", "reflect", "fingerprint", "old", "rule_reflector", "8")
            self.assertEqual(
                "old",
                _load_model_output(
                    output_dir, "context_test", "reflect", "fingerprint",
                    {"id": "rule_reflector", "version": 8},
                ),
            )
            self.assertIsNone(
                _load_model_output(
                    output_dir, "context_test", "reflect", "fingerprint",
                    {"id": "rule_reflector", "version": 11},
                )
            )

    def test_recorded_real_qwen_output_drops_conditions_duplicated_from_rule_input(self) -> None:
        # Recorded from context_000004 during a live multimodal extraction. The
        # model repeats C:/R: pairs within one rule group and uses compact source
        # phrases on the right-hand side; both forms must remain reviewable.
        expression = """规则组 1
C: 气体保护焊 | 电流密度大
R: 气体保护焊 | 电流密度大 —由于→ 弧光辐射强烈
C: 气体保护焊 | 明弧
R: 气体保护焊 | 明弧 —由于→ 弧光辐射强烈
规则组 2
C: 气体保护焊
R: 气体保护焊 —同时→ 产生焊接烟尘
"""
        ruleset = parse_final_expression(expression, "context_000004", prompt_id="rule_reflector", prompt_version="2", model="qwen")
        self.assertEqual(3, len(ruleset.rules))
        self.assertEqual([], ruleset.rules[0].conditions)
        self.assertEqual([], ruleset.rules[1].conditions)
        self.assertEqual("弧光辐射强烈", ruleset.rules[0].consequents[0].object)
        self.assertEqual("产生焊接烟尘", ruleset.rules[2].consequents[0].object)

    def test_recorded_real_qwen_output_keeps_a_genuinely_extra_constraint(self) -> None:
        ruleset = parse_final_expression(
            "规则组 1\nC: 风速|大于2m/s\nR: 气体保护焊|施焊 —[要求]→ 必须采取防风措施\n",
            "context_000004", prompt_id="rule_reflector", prompt_version="3", model="qwen",
        )
        self.assertEqual("风速", ruleset.rules[0].conditions[0].object)

    def test_gas_composition_plus_signs_stay_in_one_state(self) -> None:
        ruleset = parse_final_expression(
            "规则组1\nC: 保护气体成分|He 90% + Ar 7.5% + CO2 2.5%\n"
            "R: 焊接层次|多层焊 —[应采用]→ 保护气体成分|He 90% + Ar 7.5% + CO2 2.5%",
            "context_gas", prompt_id="rule_generator", prompt_version="9", model="qwen",
        )
        rule = ruleset.rules[0]
        self.assertEqual(1, len(rule.conditions))
        self.assertEqual("He 90% + Ar 7.5% + CO2 2.5%", rule.conditions[0].raw_state)
        self.assertEqual(1, len(rule.consequents))
        self.assertEqual("He 90% + Ar 7.5% + CO2 2.5%", rule.consequents[0].raw_state)

    def test_same_object_with_different_state_is_not_removed_as_duplicate(self) -> None:
        ruleset = parse_final_expression(
            "规则组1\nC: 保护气体|纯氩\nR: 保护气体|混入氦气 —[导致]→ 电弧压力|降低",
            "context_distinct_state", prompt_id="rule_generator", prompt_version="9", model="qwen",
        )
        self.assertEqual(1, len(ruleset.rules[0].conditions))
        self.assertEqual("纯氩", ruleset.rules[0].conditions[0].raw_state)

    def test_recorded_real_qwen_bare_r_expression_is_reviewable(self) -> None:
        ruleset = parse_final_expression(
            "规则组 1\nC: 气体空间|高温\nR: 气体空间|生成许多新的带电粒子\n",
            "context_000008", prompt_id="rule_reflector", prompt_version="2", model="qwen",
        )
        self.assertEqual("陈述", ruleset.rules[0].relation)
        self.assertEqual("生成许多新的带电粒子", ruleset.rules[0].consequents[0].raw_state)

    def test_recorded_real_qwen_rule_mislabeled_as_constraint_is_recovered(self) -> None:
        ruleset = parse_final_expression(
            "规则组 1\nC: 气体保护焊|半自动焊\nC: 风速|大于2m/s —[如果]→ 必须采取防风措施\n",
            "context_000004", prompt_id="rule_reflector", prompt_version="4", model="qwen",
        )
        self.assertEqual(1, len(ruleset.rules))
        self.assertEqual("如果", ruleset.rules[0].relation)
        self.assertEqual("半自动焊", ruleset.rules[0].conditions[0].raw_state)
