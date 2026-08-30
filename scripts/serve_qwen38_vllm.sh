#!/usr/bin/env bash
set -euo pipefail

# Verified production-oriented configuration from
# docs/qwen38_27b_inference_benchmark_20260830.md.
exec conda run -p /ENV/Anaconda/envs/jm/vllm19 --no-capture-output \
  vllm serve /DATA/jm/llms/qwen3.8-27b \
  --served-model-name /DATA/jm/llms/qwen3.8-27b \
  --host 127.0.0.1 \
  --port 8003 \
  --max-num-seqs 48 \
  --speculative-config '{"method":"mtp","num_speculative_tokens":2}'
