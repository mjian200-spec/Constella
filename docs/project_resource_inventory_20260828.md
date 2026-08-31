# Constella 项目资源梳理（2026-08-28）

## 1. 快照与边界

- 非忽略资源共 136 个文件：71 个 Python、31 个 YAML、24 个 Markdown、6 个 Web 静态文件，另有 TOML、lock、shell 和文本文件。
- 核心源码位于 `src/constella/`，共 3 条流水线、46 个 Python 源文件，约 4.5k 行。
- 测试源码 14 个文件；本次快照验证结果为 71 passed、1 skipped。
- 盘点前 Git 有 103 个 tracked 文件、36 个既有 untracked 文件；工作树中已有规则抽取和 Concept Layer 的开发中改动，本次不把这些资源解释为可删除残留。本报告本身会新增 1 个 untracked 文件。
- 大体量本地资源均被 `.gitignore` 隔离：`GMAW/` 约 380 MiB，`outputs/` 约 256 MiB；虚拟环境约 29 MiB。

## 2. 运行架构与资源流

```text
GMAW/MinerU content-list
        │
        ▼
context_builder ── document_graph.json
        │           context_packages.jsonl
        │
        ├── rule_extraction ── rulesets/*.json（逐包缓存）
        │                      structured_rules.jsonl（当前 run 权威汇总）
        │                      SQLite / Neo4j / review outputs
        │
        ├── concept_layer（Rule object 驱动）
        │      └── seeds → evidence → resolution → concepts/bindings/IS_A
        │
        └── article_discovery（文章包驱动）
               └── role → concepts/relations → audit → article concepts
```

`context_packages.jsonl` 是共享上游，但包类型有明确消费边界：

- `rule`、`formula_context` 以及旧版无类型包进入规则抽取；
- `article_candidate` 只供文章概念发现使用；
- Rule-driven Concept Layer 只读取当前规则抽取汇总中的规则。

## 3. 目录职责

| 路径 | 规模 | 生命周期 | 主要职责 |
|---|---:|---|---|
| `src/constella/context_builder/` | 15 个 Python 文件 | active | MinerU 适配、清洗、结构/资产/条件/作用域、路由、上下文包和本地 Viewer |
| `src/constella/rule_extraction/` | 18 个 Python 文件 | active | 包解析、多模态消息、提示词路由、生成/反思 DSL、状态库、Neo4j、导出和 Viewer |
| `src/constella/concept_layer/` | 11 个 Python 文件 | active, untracked | Rule-driven 概念层及独立的 article discovery 流程 |
| `configs/` | 9 个 YAML | active + inert | 模型、模式、并发、评估和 Neo4j 配置 |
| `prompts/` | 19 个 YAML | active + legacy | Context 路由、规则生成/反思、概念资格/富化/关系判断和文章包审核 |
| `scripts/` | 13 个入口 | active + utilities | 三条流水线、评估、检查、Viewer 和本地模型服务 |
| `tests/` | 14 个测试文件 | active | 单元/集成测试及依赖真实 GMAW 资源的条件测试 |
| `web/` | 2 套、6 个文件 | active | Context Builder Viewer 与 Rule Extraction Viewer |
| `docs/` | 19+ 文档 | reference/history | 设计、评估报告、问题记录和实现说明 |
| `data/rule_extraction/` | 1 个 YAML | untracked dataset | 30 条 SFT 样本，不属于运行生成物 |
| `GMAW/` | 约 380 MiB | local input, ignored | OCR JSON、PDF、图片及人工复核视觉材料 |
| `outputs/` | 20k+ 文件，约 256 MiB | generated, ignored | 多轮 Context/Rule/Concept 实验、SQLite、JSONL、缓存和报告 |

## 4. 入口脚本

### 主流程

- `scripts/build_context_packages.py`：MinerU JSON → Context Builder 输出。
- `scripts/extract_rules.py`：规则抽取、恢复/重试、可选 Neo4j、无图模式和压力清单筛选。
- `scripts/build_concept_layer.py`：从当前规则汇总构建 Rule-driven Concept Layer。
- `scripts/discover_article_concepts.py`：从文章上下文包独立发现概念与结构关系。

### 验证与检查

- `scripts/inspect_patterns.py`：模式列表、验证和单文本试配。
- `scripts/inspect_rule_extraction.py`：生成真实上下文压力测试 manifest。
- `scripts/evaluate_concept_layer.py`：Concept Layer 结构评估。
- `scripts/test_concept_layer_real.py`：真实 100 Rule / 16 并发的项目级验收入口。
- `scripts/export_rule_review_report.py`：人工规则反馈导出 Markdown。

### 服务与 Viewer

- `scripts/serve_review.py`、`scripts/serve_rule_review.py`：两套本地 Viewer。
- `scripts/serve_qwen.sh`：Qwen 3.5 9B vLLM 服务（8000）。
- `scripts/serve_gemma_transformers.py`：Gemma 多模态兼容服务（默认 8002）。

## 5. 配置和提示词对应关系

### 已生效配置

- `configs/context_builder/models.yaml`、`patterns.yaml`。
- `configs/rule_extraction/models.yaml`、`neo4j.yaml`。
- `configs/concept_layer/models.yaml`、`pipeline.yaml`、`evaluation.yaml`。

### 当前未被运行时代码加载的配置

- `configs/context_builder/pipeline.yaml`：`input_adapter`、`figure_substructure`、`allow_llm` 当前只是声明。
- `configs/rule_extraction/pipeline.yaml`：`resolver_version`、`default_workers`、`image_size_policy` 当前只是声明；实际值分别来自源码常量、CLI 默认值和未实现策略。

这些文件不应被当成有效运行开关。后续应二选一：接入 `load_runtime()` 并加配置契约测试，或移入设计文档/删除，避免“配置已改但程序不变”。

### 活跃提示词

- Context Builder：`route_classifier_v2.yaml`。
- Rule Extraction：`rule_generator_routed_base_v1.yaml`、四个 modality 模块、完整 reflector 与 protocol repair。
- Rule-driven Concept Layer：qualification、enrichment、relation classifier、direct parent resolver。
- Article discovery：package role、concept extractor、structure extractor、auditor。

### 已清理的旧提示词

Context Builder 早期 coding plan 中的 `ambiguity_resolver_v1.yaml`、
`asset_interpreter_v1.yaml` 和 `route_classifier_v1.yaml` 已确认无代码或测试引用并删除；
现行路由使用 `route_classifier_v2.yaml`，资源理解使用
`resource_textualizer_v1.yaml` 和 `formula_symbol_resolver_v1.yaml`。

## 6. 依赖与外部服务

- 核心包：Python >= 3.11、PyYAML、Neo4j driver、Pillow；由 `pyproject.toml` 和 `uv.lock` 管理。
- CUDA 模型服务：`requirements-server.txt` 单独固定 `vllm==0.19.0`，不应并入普通运行环境。
- 可选外部状态：Neo4j（默认 Bolt 7200）和本地 OpenAI-compatible 模型端点（8000/8001/8002/8003）。
- 模型配置含机器特定绝对路径 `/DATA/jm/llms/...`，换机器时必须覆盖。

## 7. 生成物与存储治理

`outputs/` 中存在大量按日期、模型、提示词版本生成的实验目录。建议采用统一 run manifest：

- 每个 run 记录 input fingerprint、代码 commit、模型、提示词版本、配置快照和状态；
- `rulesets/*.json` 明确标为缓存，`structured_rules.jsonl` 明确标为当前 run 权威汇总；
- 只把基准报告和小型评估摘要移入 `docs/`，大 JSONL、SQLite、WAL、模型输出继续留在 ignored `outputs/`；
- 对命名相近的全量目录设置保留策略，确认后再归档，禁止仅凭目录名批量删除。

当前最大的非源码资源是两份约 148–151 MiB 的 OCR PDF；最大生成文档图约 12.5 MiB。`outputs/` 内还存在 SQLite WAL，复制/归档运行结果时应连同主数据库一起处理，或先正常关闭进程/checkpoint。

## 8. 当前治理优先级

1. 将两个 inert `pipeline.yaml` 接入运行时或移出配置目录。
2. 标记/迁移 3 个 Context Builder legacy prompts。
3. 给新建的 Concept Layer 资源完成 Git 纳管审查；当前相关源码、配置、提示词、脚本和测试均为 untracked。
4. 为 article discovery 增加独立 README/输出契约，避免与 Rule-driven Concept Layer 混淆。
5. 为 `outputs/` 制定可重复实验 manifest 和归档策略，再处理历史目录。
6. 在 CI 中固定 `PYTHONPATH=src` 或安装 editable package，避免裸 `pytest` 因 src-layout 导入失败。

## 9. 不建议立即执行的清理

- 不直接删除 `outputs/`：其中包含评估报告、人工复核结果和运行状态库。
- 不直接删除无引用提示词：coding plan 仍引用旧版本，可能承担历史复现实验用途。
- 不提交 `GMAW/`、模型文件、SQLite/WAL 或 `.venv/`。
- 不在未确认新 Concept Layer 是否作为一个提交整体前拆分其 untracked 资源。
