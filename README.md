# Constella

Constella Context Builder turns MinerU document blocks into a traceable document
graph and compact context packages for downstream extraction. It deliberately
does not extract triples or infer facts that are absent from the source.

## Quick start

```bash
conda run -p /ENV/Anaconda/envs/jm/constella \
  python scripts/build_context_packages.py \
  GMAW/hybrid_ocr/'GMAW(OCR)_content_list.json' \
  --output-dir outputs/context_builder
```

The local sample data under `GMAW/` and generated `outputs/` are ignored by Git.
See `configs/context_builder/models.yaml` and `scripts/serve_qwen.sh` to use a
Qwen 3.5 9B model served by vLLM 0.19.

LLM is opt-in and is limited to low-confidence routing candidates; it never
rewrites document structure, assets, conditions, or a deterministic route.
Use one real-input batch to validate the service before a full run:

```bash
conda run -p /ENV/Anaconda/envs/jm/constella \
  python scripts/build_context_packages.py \
  GMAW/hybrid_ocr/'GMAW(OCR)_content_list.json' \
  --output-dir outputs/context_builder_llm_trial --use-llm --llm-max-batches 1
```

Known implementation decisions and runtime limitations are recorded in
[`docs/implementation_notes.md`](docs/implementation_notes.md).

## 人工审查页面

在生成输出后，启动本地审查页并在浏览器打开 `http://127.0.0.1:8765/`：

```bash
conda run -p /ENV/Anaconda/envs/jm/constella \
  python scripts/serve_review.py --output-dir outputs/context_builder
```

页面展示构建摘要、标题树、完整图表/公式及其关联、文本包的核心与支撑内容、
条件作用域和条件冲突。它只读取本地输出，不调用 LLM，也不改写 JSON 文件。

## 灵感：目录与正文标题的联合验证

目录不是独立的权威来源：OCR 可能漏页、错序或将正文编号识别为目录。后续可将目录候选与正文标题树交叉验证：以编号、标题文本和出现顺序建立候选对应；只在两者一致时提高标题等级置信度；不一致时保留原始证据并输出待检查项。该机制用于优化结构恢复，不影响当前确定性的正文标题结果。
