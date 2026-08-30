from __future__ import annotations

import json
import time
from typing import Any
from urllib.request import Request, urlopen


class LLMClient:
    """Small OpenAI-compatible client for a separately served model.

    The sglang 0.5.18 server runs in the prebuilt Docker image
    ``sglang-22.04-cu130`` (e.g. ``docker run -d --gpus device=1 --network host
    -v /DATA/jm/llms:/DATA/jm/llms sglang-22.04-cu130 -m sglang.launch_server
    ...``); the full startup command is in
    docs/qwen38_27b_inference_benchmark_20260830.md.
    """

    def __init__(self, models: dict[str, Any], event_sink=None) -> None:
        self.models = models
        self.event_sink = event_sink or (lambda **_: None)

    def complete(self, model_key: str, messages: list[dict[str, Any]], response_format: dict | None = None, **overrides: Any) -> dict[str, Any]:
        config = self.models[model_key]
        prompt_id = overrides.pop("prompt_id", None)
        prompt_version = overrides.pop("prompt_version", None)
        input_unit_ids = overrides.pop("input_unit_ids", [])
        payload = {
            "model": overrides.pop("model", config["model"]), "messages": messages,
            "temperature": overrides.pop("temperature", config.get("temperature", 0)), **overrides,
        }
        payload.update(config.get("request_options", {}))
        if response_format: payload["response_format"] = response_format
        request = Request(
            config["base_url"].rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {config.get('api_key', 'local')}"},
        )
        started = time.monotonic()
        try:
            with urlopen(request, timeout=config.get("timeout", 120)) as response:
                body = json.loads(response.read().decode())
            self.event_sink(
                task="completion", model=payload["model"], prompt_id=prompt_id,
                prompt_version=prompt_version, input_unit_ids=input_unit_ids,
                status="ok", latency=time.monotonic() - started,
            )
            return body
        except Exception as error:
            self.event_sink(
                task="completion", model=payload["model"], prompt_id=prompt_id,
                prompt_version=prompt_version, input_unit_ids=input_unit_ids,
                status=f"error:{type(error).__name__}", latency=time.monotonic() - started,
            )
            raise
