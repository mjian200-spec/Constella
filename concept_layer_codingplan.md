# Concept Layer Coding Plan（Object-only闭环版）

## 边界

- Rule不可修改。
- 只把Rule表达式中的 `object` 作为概念种子；`raw_state/normalized_state` 不参与概念分析。
- 不向外暴露Usage。Rule位置直接通过 `RuleConceptBinding` 连接Concept。
- LLM必须在证据召回之后、最终组装之前运行，不是旁路审核。
- 第一版只保存有明示证据的直接 `IS_A`；不扩展兄弟、子类或祖先。

## 模块1：Rule Object聚合

输入：Rule Extraction `rulesets/*.json`。

方法：遍历conditions、antecedents、consequents的object，以形式规范化名称聚合，并保留每个Rule位置引用。

输出：`object_seeds.jsonl`。一个ObjectSeed可关联多个RuleObjectRef。

## 模块2：多路证据召回

输入：ObjectSeed、DocumentGraph、ContextPackage。

召回通道：Rule Context精确命中、同节邻域、全文精确命中、定义句、分类句、同节语义重叠、DocumentGraph连续单元。

输出：`concept_evidence_bundles.jsonl`。召回只提供证据，不决定概念和父类。

## 模块3：证据约束Concept Resolution

输入：ObjectSeed与ConceptEvidenceBundle。

LLM分两步输出：Qualification只判断accepted/rejected/ambiguous及canonical name、alias；仅对accepted运行Enrichment，独立判断定义为supported/insufficient_evidence/ambiguous并提出直接父类候选。定义证据不足不会撤销Concept。

父类候选必须再经过独立Parent Judge。名称包含、词尾关系、领域常识和“显然是”不能证明IS_A；只接受明示分类句或表格直接层级。

输出：`concept_resolutions.jsonl`。其中同时保留全部父类候选的独立审核结果
（accepted/rejected/ambiguous、directness、证据和原因）；只有审核通过的直接父类进入组装。
这是最终组装的唯一语义决策输入。

## 模块4：Fusion与Hierarchy Assembly

只融合canonical name一致或Resolution明确给出别名的accepted结果；其余保持分离。

输出：

- `concepts.jsonl`
- `rule_concept_bindings.jsonl`
- `concept_relations.jsonl`
- `concept_layer_report.json`

父类不存在时创建depth=1 Concept；depth=1不继续扩展。

## 运行状态

无 `--use-llm` 时只生成Seed、EvidenceBundle和ambiguous Resolution，便于检查召回，不生成正式Concept。

启用 `--use-llm` 后才产生accepted Concept、Rule绑定、定义和经Parent Judge确认的直接IS_A。

## 默认真实测试

概念层的默认验证不是小样本或模拟数据，而是前100条真实Rule、Qwen 27B、16路并发，并在构建后执行严格指标评估。统一入口为 `scripts/test_concept_layer_real.py`；测试默认值记录在 `configs/concept_layer/evaluation.yaml`。少于100条的运行只能称为定向回归，不能替代默认验收。
