#!/usr/bin/env bash
set -euo pipefail

# vLLM is intentionally an explicit server process, not a pipeline side effect.
# Install requirements-server.txt into the constella environment on the CUDA host.
exec vllm serve /DATA/jm/llms/qwen3.5-9b \
  --served-model-name /DATA/jm/llms/qwen3.5-9b \
  --host 127.0.0.1 \
  --port 8000
