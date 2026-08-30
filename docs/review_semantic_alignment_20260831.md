# 图谱优化代码 Review 报告

- 日期：2026-08-31
- 范围：未提交的语义对齐/图谱优化变更（semantic_alignment 重构 + 知识图谱 viewer + auto-promotion 循环）
- 方法：多角度 review（正确性审计、语言正则、跨文件追踪、复用/简化/效率、被移除行为审计）
- 结论：34 项发现，其中 30 项已核验（CONFIRMED），4 项待确认（PLAUSIBLE）

## P0 — 数据损坏（优先修复）

| # | 位置 | 问题 | 影响 |
|---|------|------|------|
| 1 | [state_normalizer.py:13](src/constella/semantic_alignment/state_normalizer.py#L13) | `_KNOWN_UNITS` 中 `m` 排在 `ms`/`min` 前，`_UNIT` 按列表顺序匹配 | `"20ms"`/`"保温3min"` 解析为 20000mm/3000mm 长度量，损坏规范化状态值与 unit_canonical（已实际运行验证） |
| 2 | [state_normalizer.py:24](src/constella/semantic_alignment/state_normalizer.py#L24) | `_COMPARE` 词表缺 `不大于`/`不小于`，`.search()` 在否定词内部匹配到肯定词 | `"充电电流不大于60A"` 规范化为 `> 60A`，约束语义完全反转 |
| 3 | [state_normalizer.py:20](src/constella/semantic_alignment/state_normalizer.py#L20) | `_RANGE` 只允许单位在高端点后，低端点带单位的区间无法匹配 | `"1%~5%"` 被 `_SCALAR` 截断解析为下界标量，上界静默丢失；真实数据 91 条规则使用该模式 |
| 4 | [state_normalizer.py:11](src/constella/semantic_alignment/state_normalizer.py#L11) | `_NUMBER` 不支持科学计数法，指数被通用 `_UNIT` 分支吞掉 | `"大于1e3A"` → quantity=1、unit="e3A"，数量级丢失且 conversion_status=UNCHANGED 伪装为合法 |
| 5 | [registry.py:87](src/constella/semantic_alignment/registry.py#L87) | `MemorySnapshot.build` 用 `setdefault` 把任何带 type 字段的行自动标记 APPROVED | 指向旧产物时全部概念静默绕过 MODEL_GATE 审核，违背"目录行批准前都是 CANDIDATE"的核心不变量（5 个角度独立确认） |

## P1 — 流程断裂 / 注册语义错误

| # | 位置 | 问题 | 影响 |
|---|------|------|------|
| 6 | [registry.py:116](src/constella/semantic_alignment/registry.py#L116) | TYPE_REVIEW 事件只设 type 不升级 registration_status | 已批准的类型变更永远不生效（死事件），normalizer 的 TYPE_REVIEW 分支不可达，概念每轮被重复审核 |
| 7 | [registry.py:222](src/constella/semantic_alignment/registry.py#L222) | MATCHED/TYPE_REVIEW 分支要求 `not candidates` | 同名 CANDIDATE 行把已批准概念永久降级为 AMBIGUOUS；AMBIGUOUS 不产生提案，重复永远无法清除 |
| 8 | [state_normalizer.py:136](src/constella/semantic_alignment/state_normalizer.py#L136) | `_resolve` 在 surface 形式解析出 PROPOSED 时提前返回 | 未批准候选遮蔽 progressive 形式的 MATCHED 匹配，产生虚假 CONCEPT_APPROVAL 提案 |
| 9 | [assembly.py:499](src/constella/semantic_alignment/assembly.py#L499) | min_support 过滤应用到 CONCEPT_APPROVAL 提案 | 低支持度入档候选到不了 gate，循环报 NO_NEW_APPROVALS，目录永久未审核；记录降级 EXPRESSION_ONLY 却保留 candidate_concept_id |
| 10 | [viewer_server.py:445](src/constella/knowledge_graph/viewer_server.py#L445) | 文件模式读 `final_concepts.jsonl`，仓库无脚本写该文件 | 新 v2 输出目录下 viewer 启动即死；唯一能跑的目录呈现全 CANDIDATE 概念，与"已注册记忆"叙事矛盾 |
| 11 | [viewer_server.py:558](src/constella/knowledge_graph/viewer_server.py#L558) | 文件模式 search 忽略 kind=rule/state/unmatched | 同一 UI 搜索在后端切换时静默返回空结果 |
| 12 | [run_semantic_alignment_loop.py:98](scripts/run_semantic_alignment_loop.py#L98) | 只认无后缀 `alignment_proposals.jsonl` | 默认 `--max-tier H1` 产出的 seed（带 `_through_h1` 后缀）让循环启动即报错 |
| 13 | [auto_promotion.py:331](src/constella/semantic_alignment/auto_promotion.py#L331) | 批准事件硬编码 `source_seed_ids=[]`/`origin_depth=0`，且事件整体替换目录行 | 注册概念丢失种子溯源，下游 provenance/audit 无法追溯源规则 |

## P2 — 次要正确性 / UI / 测试

| # | 位置 | 问题 | 影响 |
|---|------|------|------|
| 14 | [run_semantic_alignment_loop.py:32](scripts/run_semantic_alignment_loop.py#L32) | `_report_summary` 读旧 seed 报告缺注册指标键 | initial 与 epoch-1 before 摘要记 null（已在真实 loop_report.json 验证），注册增长对比从 epoch 2 才有意义 |
| 15 | [align_semantics.py:34](scripts/align_semantics.py#L34) | 所有 trial 运行共用 `_trial` 后缀，文件名不含 limit | 同目录多次 trial 运行静默互相覆盖，无法比较 |
| 16 | [assembly.py:656](src/constella/semantic_alignment/assembly.py#L656) | `write_jsonl`/`write_json` 用无 pid 的 `.tmp`（runner/auto_promotion 均用 `.{pid}.tmp`） | 并发写同一 artifact 时 replace 竞争，崩溃或静默覆盖 |
| 17 | [app.js:482](web/knowledge_graph/app.js#L482) | ⌂"返回概念总览"按钮在 tree 模式调用 `loadHierarchy()` | 三进制分支反了，点了重渲染树而非返回总览 |
| 18 | [app.js:67](web/knowledge_graph/app.js#L67) | loadOverview/loadHierarchy/inspectTreeConcept 在 await 后不重查 viewMode | 快速切换视图时过期响应覆盖当前视图；inspectTreeConcept 误报"暂无层级"toast |
| 19 | [viewer_server.py:130](src/constella/knowledge_graph/viewer_server.py#L130) | `isolated_concepts = concept_count - len(截断后 nodes)` | 关系数超 limit 时，窗口外的已连接概念被误标为孤立 |
| 20 | [registry.py:170](src/constella/semantic_alignment/registry.py#L170) | 端点不在概念集的关系静默丢弃 | 丢失旧版 missing_relation_endpoint_count 诊断（原行为有测试覆盖），数据异常无信号 |
| 21 | [assembly.py:522](src/constella/semantic_alignment/assembly.py#L522) | unlock_count 用 `name in expression` 子串判断 | 短泛化名（如"高"）命中数百表达式，计数暴涨扭曲审核优先级 |
| 22 | [test_packages.py:61](tests/semantic_alignment/test_packages.py#L61) | 层同质性断言 `{confidence >= 0}` 恒为 `{True}` | 断言空转，从不检查 case 的 tier；未来破坏层同质性仍通过 |

## P3 — 效率

| # | 位置 | 问题 | 影响 |
|---|------|------|------|
| 23 | [packages.py:145](src/constella/semantic_alignment/packages.py#L145) | 每个 case 追加都 `json.dumps` 整个包 | 打包 O(n²)，363 包每次构建数百 MB 字符串 churn；实验网格再放大 18 倍 |
| 24 | [runner.py:252](src/constella/semantic_alignment/runner.py#L252) | 缓存验证 + `_process` 每包两次全量 dump | 缓存命中的重跑仍重序列化全部输入；可直接比对 package_id |
| 25 | [registry.py:368](src/constella/semantic_alignment/registry.py#L368) | `_signature` 每概念遍历全部 relations | 构建 O(概念×关系)≈50 万次查询，每次构建 registry 都重跑 |
| 26 | [registry.py:326](src/constella/semantic_alignment/registry.py#L326) | `lexical_coverage` 全覆盖后仍继续扫描 | 词项按长到短排序但无早退，可 `len(covered)==len(normalized)` 时 break |
| 27 | [app.js:301](web/knowledge_graph/app.js#L301) | filterHierarchy 无防抖，每键对全部节点 querySelectorAll | 5000 节点树上输入卡顿；可复用 renderHierarchy 已建的 children Map |
| 28 | [app.js:234](web/knowledge_graph/app.js#L234) | 启动即急切渲染整棵层级树 | 构建数千个用户不看的节点元素；应懒渲染子树 |

## P3 — 复用 / 可维护性

| # | 位置 | 问题 | 影响 |
|---|------|------|------|
| 29 | [auto_promotion.py:242](src/constella/semantic_alignment/auto_promotion.py#L242) | gate 整段复制 runner 的缓存/重试/原子写机制 | 两副本已分化（fingerprint 含 model 与否、raw_outputs），修复须改两处 |
| 30 | [viewer_server.py:554](src/constella/knowledge_graph/viewer_server.py#L554) | 文件后端完整重实现 KnowledgeGraphData，无共享 Protocol | 契约隐式，后端违反只能在运行时发现；文件 summary 硬编码零值误报 |
| 31 | [run_semantic_alignment_loop.py:169](scripts/run_semantic_alignment_loop.py#L169) | subprocess 转发 align_semantics CLI + 重读 JSON | 默认值两处声明会漂移，报告字段重命名运行时才炸；本可在进程内完成 |
| 32 | [align_semantics.py:125](scripts/align_semantics.py#L125) | 空选择分支手写 run_report | 已分化：decision_coverage_rate 手写 1.0 vs runner 0.0 |
| 33 | [packages.py:86](src/constella/semantic_alignment/packages.py#L86) | tier 正则硬编码比较词表 | 与 normalizer 的 `_OPERATOR_MAP` 重复，改一处另一处静默不同步 |
| 34 | [auto_promotion.py:28](src/constella/semantic_alignment/auto_promotion.py#L28) | `_NUMERIC_EXPRESSION` 重新实现 normalizer 已有的数值面检测 | 两套机制词表不同，新表达类别须同时添加两处 |

## 修复建议顺序

1. **P0 批次**（#1-5）：正则修复后有回归测试兜底（`"保温3min"`、`"不大于60A"`、`"1%~5%"`、`"1e3A"`、带 type 的 legacy 输入）；#5 需在设计层决定 registration_status 的唯一来源（建议：仅 APPROVED 事件可置 APPROVED，legacy 输入默认 CANDIDATE）
2. **P1 批次**（#6-13）：核心是注册语义一致性（#6-9 互相关联，建议一起改）+ viewer/循环的文件契约（#10-12）
3. **P2 批次**（#14-22）：小改动，可穿插
4. **P3 批次**（#23-34）：#29-32 三个"复制-已分化"问题应优先于各自的新功能开发

## 验证状态说明

- CONFIRMED：已直接核验代码（或已实际运行验证），30 项
- PLAUSIBLE：单角度发现、逻辑自洽但未逐一复现，4 项（#20、#21 计数部分、#14 已通过真实产物验证、#17-18 竞态为代码审查推断）

## 关联文件

- [docs/semantic_alignment_v2_design.md](semantic_alignment_v2_design.md) — 本批变更的设计文档（#5/#6 违反其核心不变量）
- [AGENTS.md](../AGENTS.md) — "修改输出格式时，同时检查写入端、读取端、Viewer 和恢复逻辑"（#10 违反）
