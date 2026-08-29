# Constella

Constella 是一个面向工业技术文档的、可追溯的多模态规则抽取项目。它把 MinerU 解析出的正文、标题、图片、表格和公式恢复为文档图与上下文包，再根据每个包实际包含的证据类型选择专项提示词，调用本地大模型生成规则 DSL，完成最小可解析校验、断点记录、Neo4j 入图和人工复核。

项目当前针对焊接技术资料开发，但 Context Builder、结构路由、运行状态和 Viewer 均按通用文档处理流程组织。

## 项目初衷

工业文档中的一条规则往往不只存在于一个句子中：约束可能来自标题或临近正文，结论可能位于表格，关系可能需要观察图片，公式的语义还依赖前后文。直接把整份文档交给模型会丢失来源边界，也很难判断错误发生在上下文组装、模型抽取、反思修正还是结构解析阶段。

Constella 因此把工作拆成两层：

1. Context Builder 只恢复文档结构、来源和约束作用域，构建可复查的上下文包，不凭空补事实。
2. Rule Extraction 保留宽松 DSL，根据正文、图片、表格、公式组合路由抽取，并记录模型输入输出、提示词版本、解析结果和人工反馈。

当前设计原则如下：

- 图片作为多模态上下文直接传入模型；尺寸策略暂不做先验限制，出现真实兼容性问题后再专项调整。
- 表格和公式出现在包内仅表示可供核对，不代表必须生成规则。
- 一个包始终包含正文抽取模块；存在图片、表格或公式时，再叠加相应专项模块，不在多种证据之间只选一种。
- 规则组先写一条 `C`，再写该条件下的多条 `R`；没有约束时显式写 `C: 无`。
- 反思阶段只用稀疏补丁修改、删除或补充有问题的规则，不重新生成第二份完整答案。
- 结构校验只保证最终 DSL 可解析；语义冲突和互斥条件仍由模型与人工审核处理。
- Neo4j 节点只按完全相同的身份去重；重复图片允许被不同上下文包多次处理。

## 处理流程

```text
MinerU content-list JSON
        │
        ▼
Context Builder ──► document_graph.json + context_packages.jsonl
        │
        ▼
资源解析与图片组装
        │
        ▼
正文基础提示词 + 图片/表格/公式专项提示词（按包组合）
        │
        ▼
初次抽取 ──► 稀疏反思补丁 ──► 最终 DSL 解析
        │                              │
        ├──────── 断点状态与缓存 ──────┤
        ▼                              ▼
      Neo4j                     Viewer + 人工反馈
```

## 修改历史

| 日期 | 阶段 | 主要变化 |
| --- | --- | --- |
| 2026-08-22 | Context Builder | 建立 MinerU 适配、文档图、上下文包、约束作用域和构建结果审查页。 |
| 2026-08-22 | 本地模型接入 | 接入 OpenAI 兼容的本地 vLLM 服务；低置信度上下文路由可选择使用模型。 |
| 2026-08-24 | 规则抽取 | 加入多模态模型输入、规则 DSL、反思修正、SQLite 断点状态、Neo4j 写入、终端进度和人工反馈 Viewer。 |
| 2026-08-25 | 提示词与质量验证 | 使用真实上下文包完善 few-shot、公式语义、条件查找和反思补丁，并记录压力测试与问题文档。 |
| 2026-08-25 | 结构路由 | 删除旧的统一抽取主路径，改为正文、图片、表格、公式专项提示词按资源组合；Viewer 同步展示预期/实际路由。 |
| 当前工作树 | 精简与可观测性 | 清理旧 A/B 辅助逻辑、重复反思应用和冗余状态记录；改进恢复、真实进度、路由筛选、资源分组和运行结果展示。 |

更细的设计和已知问题见 [`docs/`](docs/)，尤其是：

- [`docs/multimodal_rule_extraction_method.md`](docs/multimodal_rule_extraction_method.md)：图片、表格、公式的抽取方法与矫正方向。
- [`docs/reflection_stage_issues_20260824.md`](docs/reflection_stage_issues_20260824.md)：反思阶段问题分析。
- [`docs/issues/README.md`](docs/issues/README.md)：仍需继续解决的问题索引。
- [`rule_extraction_codingplan.md`](rule_extraction_codingplan.md)：规则抽取模块的实现计划与边界。

## 现有功能

### Context Builder

- 读取 MinerU `content_list.json`，保留页码、原始块 ID、资源路径和版面信息。
- 恢复标题树、正文、列表、图片、表格、公式及其关联。
- 生成核心内容、支撑内容、结构化资源和约束候选组成的上下文包。
- 默认确定性运行；可选用本地模型处理低置信度路由候选。
- 提供只读审查页，检查标题树、资源关联、条件作用域和歧义。
- 可选先使用 VLM 将图片和表格转为可追溯文字描述，并使用 LLM 根据公式前后文整理符号含义。
- 完整上下文包可通过 vLLM 约束选择分类为概念包、规则包、二者兼有或噪声包。

### Rule Extraction

- 每个上下文包固定启用正文模块，并按实际资源叠加 `image`、`table`、`formula` 模块。
- 图片以模型可接收的多模态消息传入；表格保留结构内容，公式保留原式与上下文语义。
- 支持 `C: 无`、一条条件对应多条结果以及宽松关系 DSL。
- 初次抽取后执行一次地址化稀疏反思，可修改条件/结果、增删规则组或在确有必要时整体替换。
- 仅把最终结果是否可解析作为硬结构门槛；不把“资源存在”误当成“必须抽取”。
- 支持由调用方配置并发 worker、终端实时进度、失败重试、模型输出刷新和断点恢复。
- 压力测试可使用真实包清单并通过 `--no-graph` 完全绕开 Neo4j，不修改数据库。
- 正式运行可写入本地 Neo4j；新运行必须显式确认清图，恢复运行不会再次清图。

### Viewer 与人工反馈

- 展示单个上下文包的标题路径、核心/支撑正文、约束、图片、表格和公式。
- 展示预期结构路由、实际提示词路由、提示词 ID/版本及全局路由分布。
- 分开展示初次抽取、反思补丁、最终 DSL 和解析状态，页面不会因进度轮询重置当前阅读位置。
- 支持按运行状态、人工审核状态、规则状态和资源路由筛选。
- 可标记结果“合适”或“不合适”；不合适时必须填写完整参考 DSL，并保存问题说明。
- 人工反馈追加保存到抽取目录，不会改写模型原始输出。

## 目录结构

```text
configs/context_builder/       Context Builder 模式与模型配置
configs/rule_extraction/       抽取模型、并发默认值和 Neo4j 配置
prompts/                       生成、专项路由与反思提示词
src/constella/context_builder/ 文档图和上下文包实现
src/constella/rule_extraction/ 路由、模型调用、反思、解析、状态与入图
scripts/                       构建、抽取、压力清单、服务和报告入口
web/review/                    Context Builder 审查页
web/rule_review/               规则抽取 Viewer
tests/                         单元测试与真实上下文回归测试
docs/                          设计记录、质量分析和问题清单
```

`GMAW/` 中的本地样本和 `outputs/` 运行产物默认不提交 Git。

## 环境准备

项目要求 Python 3.11 或更高版本。基础依赖安装后可构建上下文、访问 Neo4j 和运行 Viewer：

```bash
python -m pip install -e .
```

模型服务需要单独安装 CUDA/vLLM 依赖，避免把重量级服务依赖混入基础环境：

```bash
python -m pip install -r requirements-server.txt
```

仓库中的示例使用 Conda 环境 `/ENV/Anaconda/envs/jm/constella`。若使用其他环境，把下文命令中的 `conda run -p ...` 换成对应 Python 即可。

### 启动本地模型

`scripts/serve_qwen.sh` 默认启动 Qwen 3.5 9B（端口 `8000`）。结构路由全量抽取当前使用的模型键和地址在 [`configs/rule_extraction/models.yaml`](configs/rule_extraction/models.yaml) 中配置；例如 `qwen3_8_27b` 对应端口 `8003`。

```bash
bash scripts/serve_qwen.sh
```

使用其他本地模型时，启动一个 OpenAI Chat Completions 兼容服务，并同步修改模型路径、端口和 `--model-key`。模型必须支持当前包可能包含的图片输入。

### 启动本地 Neo4j

当前数据库目录位于 `/DATA/jm/neo4j/CollestaGraph`，Bolt 端口由该目录下的 `conf/` 配置决定；仓库目前连接 `bolt://127.0.0.1:7200`，数据库名和用户名均为 `neo4j`。密码只通过环境变量传入，不写入配置或 README：

```bash
export CONSTELLA_NEO4J_PASSWORD='你的本地密码'
```

如本地 Bolt 端口变化，请修改 [`configs/rule_extraction/neo4j.yaml`](configs/rule_extraction/neo4j.yaml)。压力测试使用 `--no-graph` 时无需启动 Neo4j，也无需设置密码。

## 运行介绍

以下命令均在仓库根目录执行。

### 1. 从原始文档重新构建上下文包

建议每次正式全量抽取先使用新输出目录重新构建，避免把旧上下文缓存混入新运行：

```bash
conda run -p /ENV/Anaconda/envs/jm/constella \
  python scripts/build_context_packages.py \
  'GMAW/hybrid_ocr/GMAW(OCR)_content_list.json' \
  --output-dir outputs/context_builder
```

只有需要模型判别低置信度上下文路由时才加 `--use-llm`。可先限制一个真实批次验证服务：

```bash
conda run -p /ENV/Anaconda/envs/jm/constella \
  python scripts/build_context_packages.py \
  'GMAW/hybrid_ocr/GMAW(OCR)_content_list.json' \
  --output-dir outputs/context_builder_llm_trial \
  --use-llm --llm-max-batches 1
```

资源理解和最终包路由使用独立开关，避免一次试跑意外调用全部资源和上下文包。建议先限制资源数量验证多模态服务：

```bash
conda run -p /ENV/Anaconda/envs/jm/constella \
  python scripts/build_context_packages.py \
  'GMAW/hybrid_ocr/GMAW(OCR)_content_list.json' \
  --output-dir outputs/context_builder_semantic_trial \
  --use-resource-llm --resource-max-items 10 \
  --use-package-router
```

`--use-resource-llm` 会把真实图像作为 `image_url` 内容块发送给配置的视觉模型；表格同时发送原始表格正文。`--use-package-router` 对建好的每个包使用 vLLM `structured_outputs.choice`，只允许输出 `C/R/B/N`，分别对应概念、规则、二者兼有和噪声。两阶段结果都按输入、模型和 Prompt 指纹缓存。

查看 Context Builder 结果：

```bash
conda run -p /ENV/Anaconda/envs/jm/constella \
  python scripts/serve_review.py --output-dir outputs/context_builder
```

浏览器打开 `http://127.0.0.1:8765/`。

### 2. 在调用模型前检查资源解析

该命令只解析上下文包和资源路径，不调用模型、不创建抽取状态、不连接 Neo4j：

```bash
conda run -p /ENV/Anaconda/envs/jm/constella \
  python scripts/extract_rules.py \
  --context-output-dir outputs/context_builder \
  --output-dir outputs/rule_extraction_resolve_check \
  --dry-run-resolve --progress
```

如果 MinerU JSON 中的图片路径不是相对当前文档目录可解析的路径，可显式增加 `--asset-root /实际资源根目录`。

### 3. 真实上下文压力测试（不接触 Neo4j）

先生成可复现的真实上下文清单。清单综合固定难例、长文本、约束数量和图片/表格/公式配额：

```bash
conda run -p /ENV/Anaconda/envs/jm/constella \
  python scripts/inspect_rule_extraction.py \
  --context-output-dir outputs/context_builder \
  --output outputs/rule_extraction_stress/stress_manifest.json \
  --count 40
```

再以 16 个持续补位的 worker 运行模型抽取；一个任务结束后会立即领取下一个任务：

```bash
conda run -p /ENV/Anaconda/envs/jm/constella \
  python scripts/extract_rules.py \
  --context-output-dir outputs/context_builder \
  --output-dir outputs/rule_extraction_stress \
  --manifest outputs/rule_extraction_stress/stress_manifest.json \
  --model-key qwen3_8_27b \
  --workers 16 --progress --no-graph --refresh-model-output
```

该流程会执行真实生成、反思、解析和导出，但不会读取、清空或写入 Neo4j。

### 4. 正式全量抽取并写入 Neo4j

首次全量运行使用一个新的抽取输出目录，并显式传入 `--reset-graph`：

```bash
conda run -p /ENV/Anaconda/envs/jm/constella \
  python scripts/extract_rules.py \
  --context-output-dir outputs/context_builder \
  --output-dir outputs/rule_extraction_full \
  --model-key qwen3_8_27b \
  --workers 16 --progress --reset-graph --refresh-model-output
```

> `--reset-graph` 会清空配置所指 Neo4j 数据库中的既有 Constella 图数据，是显式的破坏性操作。它不能与 `--resume` 同时使用。

仅测试指定包时可重复传入 `--package-id context_000001`，或使用 `--limit N`。这类参数只改变选择范围，不改变结构路由规则。

### 5. 断点恢复与失败重试

运行中断后，使用完全相同的上下文输出目录、抽取输出目录和模型配置恢复：

```bash
conda run -p /ENV/Anaconda/envs/jm/constella \
  python scripts/extract_rules.py \
  --context-output-dir outputs/context_builder \
  --output-dir outputs/rule_extraction_full \
  --model-key qwen3_8_27b \
  --workers 16 --progress --resume
```

默认不会重新执行已经失败的包；需要重试失败项时增加 `--retry-failed`。如果还需废弃已有模型响应并重新请求，再增加 `--refresh-model-output`。

输入指纹变化时程序会拒绝恢复，以免把不同版本的上下文结果混入同一运行。提示词路由或输出协议发生变化时，也应使用新的输出目录开始新运行。

### 6. 启动规则抽取 Viewer

Viewer 可以在抽取过程中启动，进度来自当前运行的状态文件，页面以局部轮询更新摘要，不会自动切换当前正在阅读的上下文包：

```bash
conda run -p /ENV/Anaconda/envs/jm/constella \
  python scripts/serve_rule_review.py \
  --context-output-dir outputs/context_builder \
  --extraction-output-dir outputs/rule_extraction_full
```

浏览器打开 `http://127.0.0.1:8766/`。若只审核压力测试清单，可附加：

```text
--manifest outputs/rule_extraction_stress/stress_manifest.json
```

## 输出文件

Context Builder 输出：

| 文件 | 内容 |
| --- | --- |
| `document_graph.json` | 可追溯文档单元、关系、约束、歧义和来源信息。 |
| `context_packages.jsonl` | 下游抽取的上下文包。 |
| `ontology_candidates.jsonl` | 中性的结构化候选记录；文件名为兼容既有输出协议而保留。 |
| `ambiguities.jsonl` | 待核对的文档结构歧义。 |
| `run_report.json` | 构建数量、模型调用和耗时摘要。 |

Rule Extraction 输出：

| 文件/目录 | 内容 |
| --- | --- |
| `rulesets/<context_id>.json` | 每个上下文包的最终可解析规则集。 |
| `structured_rules.jsonl` | 成功规则的扁平化汇总。 |
| `processed_context_packages.jsonl` | 包状态、规则 ID 和失败信息。 |
| `rule_extraction_report.json` | 本次运行、路由分布、成功/无规则/失败数量和耗时。 |
| `rule_extraction_state.sqlite3` | 运行、包阶段和模型调用状态，用于进度与恢复。 |
| `cache/contexts/` | 已解析上下文及资源清单。 |
| `cache/model_outputs/` | 初次抽取和反思阶段的原始模型输出及提示词元数据。 |
| `manual_feedback.jsonl` | Viewer 追加的人工结论、参考答案和问题说明。 |

## 测试

基础测试不需要启动模型或 Neo4j：

```bash
conda run -p /ENV/Anaconda/envs/jm/constella python -m unittest discover -s tests -v
```

真实上下文回归测试会自动读取本地 `GMAW/` 样本；样本不存在时相应测试会跳过。模型质量不能仅靠格式测试判定，正式提示词变更应至少执行一次真实包压力测试，并在 Viewer 中逐项人工检查核心原文与参考答案。

## 当前边界

- DSL 暂不限制为封闭语法，只要求最终结果可解析。
- 暂不自动裁决跨规则语义冲突、互斥条件或知识正确性。
- 公式只有在原文明确表达等价、推导、简化等关系时，才应把公式放在关系两端；公式只是参数或条件时不应制造公式自环。
- 图片 OCR 与视觉内容冲突、复杂公式语义和隐含约束仍是主要人工审核区域。
- Viewer 的人工反馈目前用于记录参考答案，不会自动回写模型提示词或修改 Neo4j。
