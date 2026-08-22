# Context Builder implementation notes

## Deliberate decisions

- `GMAW(OCR)_content_list.json` is the first input adapter. It has no immutable
  original block identifier, so the adapter creates a stable ID from the input
  file stem and the original array index, while preserving `page_idx`, `bbox`,
  and asset path.
- Figures remain complete asset units. This is an agreed scope: MinerU provides
  a bitmap and caption but not reliable curve, region, or axis structures. A
  curve/region reference creates an ambiguity instead of invented subunits.
- Repeated side-by-side table headers are expanded into logical rows before
  alignment. This prevents a number in the right half of an OCR row from
  leaking into the left-half condition package.
- The pipeline is deterministic by default. The vLLM/OpenAI-compatible client,
  model configuration, versioned prompts, and launch script are supplied for
  optional low-confidence routing and ambiguity resolution; no model call is
  made unless a later routing policy explicitly selects a low-confidence batch.

## vLLM runtime record on 2026-08-22

`vllm==0.19.0` installs correctly and recognises the local model as
`Qwen3_5ForConditionalGeneration`. The first service launch could not start
because both GPUs were occupied: GPU 0 had 9,025 MiB free and GPU 1 had 6,267
MiB free, while vLLM requested about 85.47 GiB at its default 0.9 memory
utilisation. No unsafe memory reduction was attempted.

After the user authorised termination of the three existing vLLM engine
processes, `scripts/serve_qwen.sh` started successfully on GPU 0. The
OpenAI-compatible model-list endpoint and a `chat/completions` request both
completed successfully at `http://127.0.0.1:8000/v1`.
