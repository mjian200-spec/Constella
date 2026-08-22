# Context Builder implementation notes

## Deliberate decisions

- `GMAW(OCR)_content_list.json` is the first input adapter. It has no immutable
  original block identifier, so the adapter creates a stable ID from the input
  file stem and the original array index, while preserving `page_idx`, `bbox`,
  and asset path.
- Figures and tables remain complete asset units. This is an agreed scope:
  MinerU provides a bitmap, table HTML, and captions, but the Context Builder
  does not create curve, region, axis, row, column, or cell subunits.
- Explicit IDs, conservative caption-text matches, and same-section relative
  references can link text to a complete asset. Competing reference candidates
  are retained on the Unit as candidates, not recorded as ambiguities.
- `Ambiguity` is reserved for conflict between conditions. An uncertain asset
  reference never becomes an ambiguity record.
- When an exact `目录` marker is followed by a completed chapter sequence and a
  later non-header `第1章`, the later reset marks the body start; the TOC and
  all preceding Units are discarded. If that reset evidence is absent, the
  pipeline keeps the document unchanged rather than guessing a body boundary.
- Numbered headings require supporting hierarchy or local sibling progression.
  Page headers, MinerU `aside_text` page-side text, isolated dotted numbers, and simple ordinal runs such as
  `1）`/`2）` remain passages. This intentionally favours a smaller, more
  reliable title tree over recovering every list-level heading.
- A context package includes conditions from earlier text within scope and
  from its containing title path. It deliberately excludes a condition found
  in the core rule passage itself, because downstream LLM extraction will
  interpret that sentence directly.
- The causal rule accepts both an intervening phrase (例如“将会显著增加”) and a
  direct modal-result expression (例如“将产生气孔”); the latter is covered by a
  dedicated regression test.
- The pipeline is deterministic by default. With `--use-llm`, only complete
  low-confidence passage-route candidates are sent in batches to the local
  Qwen service. The model can select `rule`, `structured_candidate`, `support`,
  or `unknown`; its choice cannot alter document structure, asset links,
  conditions, scopes, or a deterministic route. Each call records model,
  prompt ID/version, input Unit IDs, status and latency in `run_report.json`.
- The first real GMAW trial used `route_classifier_v2` on 12 of 938 eligible
  candidates. Qwen returned `unknown` for every supplied Unit, so the trial
  created no additional rule package. This is retained as a valid conservative
  result, not interpreted as a missing fact or silently promoted classification.

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
