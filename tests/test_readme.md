# Context Builder 测试说明

## 测试输入约束

所有自动化测试均以 `GMAW/hybrid_ocr/GMAW(OCR)_content_list.json` 为唯一文档输入，
通过其原始数组索引定位证据。测试不构造文本、标题、图表、公式、条件或图片等合成文档片段。
若本地不存在该被 Git 忽略的原始文件，测试会跳过，而不是以虚构替代数据运行。

## 测试级别

本项目当前自动化测试全部是**强测试**：每次运行完整真实文档管线，并对正式输出做断言。
弱测试和合成输入测试均已移除。网页视觉呈现由人工在浏览器中核验。

## 当前测试清单

| 名称 | 级别 | 原始依据 | 通过标准 |
|---|---|---|---|
| `test_real_output_contract` | 强 | 全部原始输入 | 正文 Unit 数等于原始块数减去目录前置块数；每个规则 Unit 生成一个规则包，显式公式引用可另生成公式上下文包；只保留完整资产；正式输出文件齐全。 |
| `test_toc_and_front_matter_use_real_reset_evidence` | 强 | 索引 121 `目录`、131 `第1章绪论` | 目录后的章节序列重置定位正文，前 131 个 Unit 全部删除。 |
| `test_real_heading_evidence_rejects_headers_asides_and_ordinal_runs` | 强 | 索引 132、140、488、1510、3025、152、157、2559 | `1.1`、`1.2`、`2.2`、`3.3`、`5.4` 保留为标题；页眉、页侧文字、`1）`序号不成为标题。 |
| `test_real_table_formula_and_caption_assets_remain_whole_and_linked` | 强 | 索引 614、641、644、2488、2491、2492、2493、2497 | 同一原文中的图 2-25 与式（2-38）必须同时关联；整表保留；式（5-21）建立双向公式关联，含该公式的文本包必须包含完整公式和其连续的两段原始符号说明；题注描述只关联完整图。 |
| `test_real_formula_introduction_keeps_the_following_formula_in_the_package` | 强 | 索引 1471、1472、1473 | 原文“关系式为”即使同段还引用图 3-56，也必须关联紧随的完整公式（3-18），并纳入该规则包。 |
| `test_real_relative_reference_and_direct_modal_result` | 强 | 索引 718、734、2522 | 原文“下表”建立同节相对关联；原文“否则将产生气孔”生成因果规则候选。 |
| `test_real_structured_candidate_output_uses_classification_source` | 强 | 索引 141 | 原文分类语句进入结构化候选输出，并保留原始块标识。 |
| `test_real_material_condition_is_terminated_by_the_next_material_condition` | 强 | 索引 381、461 | 同一原文段的后一材料条件立即终止前一材料条件；后一条件作用至下一节之前。 |
| `test_real_scopes_and_packages_exclude_core_conditions` | 强 | 索引 2522 及全部真实文本包 | 核心段自身条件不进入包；每个包中的文本条件均来自其前文，标题条件来自所属标题路径。 |
| `test_one_real_batch_is_auditable_and_candidate_bounded` | 强（按需） | 完整原始输入；实际 vLLM 单批候选 | 真实 Qwen 调用成功；记录模型、Prompt 版本、原始 Unit ID、状态和耗时；返回角色被限制在允许集合。 |

## 运行命令

```bash
conda run -p /ENV/Anaconda/envs/jm/constella \
  python -m unittest tests.context_builder.test_pipeline -v
```

启用本机 Qwen 服务后，运行一次真实 LLM 强测试：

```bash
CONSTELLA_TEST_LLM=1 conda run -p /ENV/Anaconda/envs/jm/constella \
  python -m unittest tests.context_builder.test_llm_real_gmaw -v
```

## 网页人工核验

```bash
conda run -p /ENV/Anaconda/envs/jm/constella \
  python scripts/serve_review.py --output-dir outputs/context_builder
```

在浏览器打开 `http://127.0.0.1:8765/`，核验标题树、完整图表/公式、文本包及条件范围。
