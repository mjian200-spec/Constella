from __future__ import annotations

import json
import time
from typing import Any
from urllib.request import Request, urlopen


class LLMClient:
    """Small OpenAI-compatible client for a separately served vLLM model."""

    def __init__(self, models: dict[str, Any], event_sink=None) -> None:
        self.models = models
        self.event_sink = event_sink or (lambda **_: None)

    def complete(self, model_key: str, messages: list[dict[str, str]], response_format: dict | None = None, **overrides: Any) -> dict[str, Any]:
        config = self.models[model_key]
        payload = {
            "model": overrides.pop("model", config["model"]), "messages": messages,
            "temperature": overrides.pop("temperature", config.get("temperature", 0)), **overrides,
        }
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
            self.event_sink(task="completion", model=payload["model"], prompt_id=overrides.get("prompt_id"), prompt_version=overrides.get("prompt_version"), status="ok", latency=time.monotonic() - started)
            return body
        except Exception as error:
            self.event_sink(task="completion", model=payload["model"], prompt_id=overrides.get("prompt_id"), prompt_version=overrides.get("prompt_version"), status=f"error:{type(error).__name__}", latency=time.monotonic() - started)
            raise
