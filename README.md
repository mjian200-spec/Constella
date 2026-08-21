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

Known implementation decisions and runtime limitations are recorded in
[`docs/implementation_notes.md`](docs/implementation_notes.md).
