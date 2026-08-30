# Semantic Alignment v2：数据状态与递进记忆规范

## 1. 模块边界

语义对齐模块是只读的语义记录编译器。它读取规则、候选概念目录和正式注册记忆，输出对象语义、状态语义、晋升提案和派生画像；不在对齐阶段创建、融合、删除概念，不修改规则，也不写入Neo4j。

`outputs/article_concepts_full_20260829`中的720个条目都是文章模型发现的`CANDIDATE`，不是审核通过概念。它们只参与召回和自动准入判断。只有完整概念身份与类型同时通过准入门、以`registration_status=APPROVED`写入版本化记忆后，才属于正式注册中心，才允许产生`MATCHED`和画像。

概念只保存跨规则稳定的种类身份：`concept_id`、名称、别名、`object|state`类型、定义、`IS_A|PART_OF|SAME_AS`和证据指针。原文、参数、条件和限定保存在语义记录。

## 2. 正交状态

对象结构和概念对齐是两个正交维度：

- `structure`：`ATOMIC | COMPOSED | UNRESOLVED`
- `alignment_status`：`MATCHED | PARTIAL | AMBIGUOUS | PROPOSED | TYPE_REVIEW | EXPRESSION_ONLY`

`COMPOSED + MATCHED`表示表达包含多个语义部分，且所有部分均已引用正式注册概念。候选目录中的精确命中仍为`PROPOSED`并生成`CONCEPT_APPROVAL`，不能伪装成强匹配。只有身份已批准但缺少类型的遗留注册概念才使用`TYPE_REVIEW`。

## 3. 对象语义记录

每个源`state_id`恰好对应一条对象语义记录：

```yaml
record_id: object_semantic_...
source_state_id: state_...
interpretation_id: object_interpretation_...
raw_object: 温度超过60°C时充电中的电池组
structure: COMPOSED
alignment_status: MATCHED
core_objects:
  - text: 电池组
    concept_id: concept_...
intrinsic_state_record_ids: [state_semantic_...]
condition_record_ids: [state_semantic_...]
qualifiers: []
source_rule_ids: []
context_package_ids: []
memory_version: memory_...
```

对象解释按规范化对象表达缓存为模板，再确定性展开到所有源状态，兼顾LLM调用量和规则级追溯。

## 4. 状态语义记录

状态记录统一使用一种结构，通过`semantic_role`区分来源：

- `RULE_VALUE`：规则原始`raw_state`
- `OBJECT_INTRINSIC_STATE`：对象内部、主语为核心对象的状态
- `RULE_CONDITION`：主语为外部量、环境或部件的条件

```yaml
record_id: state_semantic_...
source_state_id: state_...
semantic_role: RULE_CONDITION
raw_object: 温度
raw_state: 超过60°C
canonical_surface: 温度>{quantity}
subject_object_concept_ids: [concept_temperature]
state_concept_id: null
operator_family: ">"
quantity:
  value: "333.15"
  unit_original: °C
  unit_canonical: K
  precision: 2
  inclusive: false
qualifiers:
  - dimension: 温度
alignment_status: EXPRESSION_ONLY
source_rule_ids: []
```

温标必须进行数值换算；`60°C`精确换算为`333.15K`。`≥`与`>`通过`inclusive`区分，不能无损合并。

## 5. 审核提案

提案独立落盘，类型为：

- `OBJECT_CONCEPT`
- `STATE_CONCEPT`
- `CONCEPT_APPROVAL`
- `NORMALIZATION_PATTERN`
- `ALIAS`
- `TYPE_REVIEW`

数值、比较算子和“增加、降低、高、较大”等变化/程度表达不能直接成为概念，进入`NORMALIZATION_PATTERN`。提案在形态归一、参数剥离、类型判定和同对象维度聚合之后再计算support。

## 6. 置信度分级

- `H0`：唯一、类型一致的精确原子匹配，完全确定性处理
- `H1`：已知概念覆盖充分、结构简单、歧义低
- `H2`：部分已知或存在少量歧义
- `H3`：复杂嵌套、低覆盖、噪声或主语不清

package不得混合置信等级。package优先级为：置信等级、结构复杂度、上下文支持度、出现频率。package级置信度取内部最低case，不能用平均值掩盖困难case。

## 7. 审核记忆与epoch

同一并发批次冻结使用一个只读记忆快照`Mn`。模型准入门对候选概念同时检查稳定种类、非实例/参数、单一身份、文章证据充分和类型清晰；只有五项全部通过且模型置信度为`HIGH`时，才写入完整的APPROVED概念事件。下一次运行加载它们形成`Mn+1`，重建索引并重新评分。

未审核LLM输出、规则参数、限定和`EXPRESSION_ONLY`不能进入记忆。缓存指纹必须包含`memory_version`，避免并发顺序影响结果。

## 8. 产物

正式产物固定为：

1. `object_semantics.jsonl`
2. `state_semantics.jsonl`
3. `alignment_proposals.jsonl`
4. `state_coverage.jsonl`
5. `alignment_report.json`

报告必须记录schema、prompt、输入和记忆指纹，并验证：每个源状态恰好一条对象记录和一条`RULE_VALUE`记录；派生状态均有来源；原文不变；源频率守恒；所有非空概念ID属于当前记忆；概念数量不因对齐运行而变化。

## 9. 自动递进运行方法

单次对齐默认只运行到`H1`。自动循环使用已有全量结果收集第一批候选，然后执行“准入→冻结记忆→H3全量重跑→再次准入”，直到没有新批准或达到epoch上限：

```bash
# 只构建索引、分级和package，不调用模型
python scripts/align_semantics.py --dry-run

python scripts/run_semantic_alignment_loop.py \
  --seed-artifact-dir outputs/semantic_alignment_content_v2_full_20260830 \
  --output-dir outputs/semantic_alignment_auto_loop_20260831 \
  --max-epochs 2
```

自动准入写入的是完整概念事件，而不是给720个候选批量补类型：

```json
{"status":"APPROVED","proposal_kind":"CONCEPT_APPROVAL","approval_mode":"MODEL_GATE","concept":{"concept_id":"concept_xxx","canonical_name":"电弧","aliases":[],"definition":"...","type":"object","registration_status":"APPROVED","evidence_ids":["unit_x"]}}
```

加载记忆后会生成新的`memory_version`，旧package缓存不会被误用；尚未完成的对象按新注册中心重新计算置信度和package。准入决策、未批准原因、Prompt、模型、基础记忆版本和每轮指标均独立落盘。新概念提案暂不自动创建，因为当前语义提案还没有绑定到足够的原始证据单元；它们继续进入下一轮候选收集，不污染正式注册中心。

提案带`support`、`unlock_count`和`review_priority=P0..P3`。候选准入以及同时具有高support和高解锁价值的概念最先处理；变化词和数量模式默认位于低优先级，不能直接晋升为概念。

## 10. 评价指标

评价分四层，不能用一个混合总分掩盖语义错误：

| 层级 | 主指标 | 用途 |
| --- | --- | --- |
| 人工金标准 | 对象结构准确率、核心概念Precision/Recall/F1及集合全对率、状态概念准确率、算子边界准确率、规范量值准确率 | 判断语义是否正确，是发布门禁 |
| 候选检索 | candidate recall、按源频率加权candidate recall、分H0-H3召回 | 判断正确概念是否进入LLM候选集 |
| 运行协议 | package成功率、decision覆盖率、缓存命中率、失败数 | 判断输出是否完整可恢复 |
| 成本与审核 | package数、输入字符总量及P95、匹配率、提案压缩率、记忆后的tier晋升率和机械处理增量 | 优化调用成本和人工审核收益 |

旧版对齐结果只能作为弱标签评估候选检索，且仅保留仍指向当前冻结注册中心的`ALIGNED`记录。它不能用于报告语义准确率，也不能替代人工金标准。

人工金标准为JSONL。对象与状态记录示例：

```json
{"record_type":"object","source_state_id":"state_1","structure":"COMPOSED","core_concept_ids":["battery"]}
{"record_type":"state","source_state_id":"state_1","semantic_role":"RULE_CONDITION","raw_object":"温度","raw_state":"超过60°C","state_concept_id":"overheat","operator_family":">","quantity":{"value":"333.15","unit_canonical":"K","inclusive":false}}
```

缺失预测按错误计入准确率，同时单独报告record coverage，防止通过少输出获得虚高分。数值比较在统一单位后使用`1e-6`绝对容差。

## 11. 实验与持续优化闭环

先建立一份按H0-H3和高频/长尾分层抽样、双人复核分歧的固定金标准。每次修改遵循同一闭环：

1. 固定输入指纹、记忆版本、prompt版本和金标准版本；
2. 先跑确定性单测与全量不变量，再跑候选/分包网格；
3. 候选参数以加权召回为首要约束，在召回差不超过容差的方案中最小化package数、总字符和P95；
4. 只对通过金标准门禁的方案进行真实模型A/B；
5. 审核一批提案形成新记忆，比较tier晋升和机械处理增量；
6. 保存实验JSON，确认改善不是来自输入、记忆或缓存变化后再修改默认值。

运行离线评估：

```bash
python scripts/evaluate_semantic_alignment.py \
  --artifact-dir outputs/semantic_alignment_content_v2 \
  --artifact-suffix _through_h1 \
  --gold tests/fixtures/semantic_alignment_gold.jsonl
```

运行不调用模型的候选和分包网格：

```bash
python scripts/experiment_semantic_alignment.py \
  --candidate-grid 4,6,8 \
  --objects-grid 8,12,16 \
  --chars-grid 25000,40000 \
  --output outputs/semantic_alignment_experiment.json
```

建议的初始发布门禁是：所有数据不变量通过；协议成功率和decision覆盖率均为100%；候选加权召回不低于当前基线；人工金标准的结构、核心概念F1、状态概念、算子边界和量值指标均不得回退。积累至少两轮稳定实验后，再为各项准确率设置绝对阈值，避免在金标准样本尚小时制造虚假的精确目标。

2026-08-30的首轮弱标签网格覆盖`candidate=4/6/8`、`objects=8/12/16`和`max_chars=25000/40000`，与当前720个概念相交得到956条弱标签。初始候选器推荐`8/16/40000`，candidate recall为`0.9121`，按源频率加权为`0.9712`，生成363个package。

第二轮将候选器改为“包含名称/别名优先 + 名称别名N-gram + 富语义N-gram补充”。同一输入、标签和网格下，推荐组合变为`6/16/40000`：candidate recall为`0.9163`（`+0.0042`），加权召回为`0.9909`（`+0.0197`），package数保持363，总输入字符从`11,466,782`降为`9,735,529`（`-1,731,253`）。8候选有`0.9921`的加权召回，但与6候选的差为`0.0012`，小于`0.002`容差，因此按成本规则选择6候选作为主入口默认值。该结论仍需固定人工金标准验证，尤其是H2长尾；不能将弱标签召回解释为语义准确率。
