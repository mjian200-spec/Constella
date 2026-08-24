#!/usr/bin/env python3
"""Serve Gemma 4 through the small OpenAI-compatible API used by Constella.

This adapter exists because the native Gemma 4 checkpoint is supported by
Transformers before it is supported by the installed vLLM weight loader.  It
keeps images in the request and serializes generation on a single GPU.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import queue
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import torch
from PIL import Image
from transformers import AutoModelForMultimodalLM, AutoProcessor


@dataclass
class PendingRequest:
    payload: dict[str, Any]
    done: threading.Event
    result: str | None = None
    error: BaseException | None = None


class GemmaBackend:
    def __init__(self, model_path: str, batch_size: int = 1, batch_window_ms: int = 100) -> None:
        self.model_path = model_path
        self.processor = AutoProcessor.from_pretrained(model_path, local_files_only=True)
        self.model = AutoModelForMultimodalLM.from_pretrained(
            model_path,
            dtype="auto",
            device_map="auto",
            local_files_only=True,
            low_cpu_mem_usage=True,
        )
        self.model.eval()
        self.batch_size = batch_size
        self.batch_window_seconds = batch_window_ms / 1000
        self.requests: queue.Queue[PendingRequest] = queue.Queue()
        threading.Thread(target=self._batch_worker, name="gemma-batch-worker", daemon=True).start()

    @staticmethod
    def _decode_image(url: str) -> Image.Image:
        if not url.startswith("data:") or "," not in url:
            raise ValueError("Only embedded data URL images are supported")
        header, encoded = url.split(",", 1)
        if ";base64" not in header:
            raise ValueError("Image data URL must be base64 encoded")
        return Image.open(io.BytesIO(base64.b64decode(encoded))).convert("RGB")

    def _convert_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        for message in messages:
            content = message.get("content", "")
            if isinstance(content, str):
                converted.append({"role": message["role"], "content": [{"type": "text", "text": content}]})
                continue
            blocks: list[dict[str, Any]] = []
            # Gemma recommends images before text. Preserve the relative order
            # within each kind while moving all images to the front.
            for block in content:
                if block.get("type") == "image_url":
                    image_url = block.get("image_url", {})
                    url = image_url.get("url") if isinstance(image_url, dict) else image_url
                    blocks.append({"type": "image", "image": self._decode_image(str(url))})
            for block in content:
                if block.get("type") == "text":
                    blocks.append({"type": "text", "text": str(block.get("text", ""))})
            converted.append({"role": message["role"], "content": blocks})
        return converted

    @staticmethod
    def _content_from_parsed(parsed: Any, fallback: str) -> str:
        if isinstance(parsed, dict):
            content = parsed.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                texts = [str(item.get("text", "")) for item in content if isinstance(item, dict) and item.get("type") == "text"]
                if texts:
                    return "".join(texts)
        return fallback

    def _complete_batch(self, pending: list[PendingRequest]) -> list[str]:
        conversations = [self._convert_messages(item.payload.get("messages", [])) for item in pending]
        max_new_tokens = max(int(item.payload.get("max_tokens") or 4096) for item in pending)
        temperatures = {float(item.payload.get("temperature", 0)) for item in pending}
        if len(temperatures) != 1:
            raise ValueError("All requests in a generation batch must use the same temperature")
        temperature = temperatures.pop()
        with torch.inference_mode():
            inputs = self.processor.apply_chat_template(
                conversations,
                tokenize=True,
                return_dict=True,
                return_tensors="pt",
                add_generation_prompt=True,
                enable_thinking=False,
                padding=True,
            ).to(self.model.device)
            input_len = inputs["input_ids"].shape[-1]
            generation: dict[str, Any] = {"max_new_tokens": max_new_tokens}
            if temperature > 0:
                generation.update(do_sample=True, temperature=temperature, top_p=0.95, top_k=64)
            else:
                generation["do_sample"] = False
            outputs = self.model.generate(**inputs, **generation)
            results: list[str] = []
            for output in outputs:
                generated = output[input_len:]
                decoded = self.processor.decode(generated, skip_special_tokens=False)
                fallback = self.processor.decode(generated, skip_special_tokens=True).strip()
                try:
                    parsed = self.processor.parse_response(decoded)
                except Exception:
                    parsed = None
                results.append(self._content_from_parsed(parsed, fallback).strip())
            return results

    def _batch_worker(self) -> None:
        while True:
            first = self.requests.get()
            pending = [first]
            deadline = time.monotonic() + self.batch_window_seconds
            while len(pending) < self.batch_size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    pending.append(self.requests.get(timeout=remaining))
                except queue.Empty:
                    break
            try:
                results = self._complete_batch(pending)
                for item, result in zip(pending, results, strict=True):
                    item.result = result
            except BaseException as error:
                for item in pending:
                    item.error = error
                traceback.print_exc()
            finally:
                for item in pending:
                    item.done.set()
                    self.requests.task_done()

    def complete(self, payload: dict[str, Any]) -> str:
        pending = PendingRequest(payload=payload, done=threading.Event())
        self.requests.put(pending)
        pending.done.wait()
        if pending.error is not None:
            raise pending.error
        return pending.result or ""


def handler_for(backend: GemmaBackend):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ConstellaGemma/1.0"

        def _json(self, status: int, value: dict[str, Any]) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # The model may finish after an HTTP client timeout. The
                # generated result is intentionally discarded in that case.
                pass

        def do_GET(self) -> None:  # noqa: N802
            if self.path.rstrip("/") == "/v1/models":
                self._json(200, {"object": "list", "data": [{"id": backend.model_path, "object": "model"}]})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path.rstrip("/") != "/v1/chat/completions":
                self._json(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length))
                started = time.monotonic()
                content = backend.complete(payload)
                completion_id = "chatcmpl-" + uuid.uuid4().hex
                self._json(200, {
                    "id": completion_id,
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": backend.model_path,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                    "latency_seconds": round(time.monotonic() - started, 3),
                })
            except Exception as error:
                traceback.print_exc()
                self._json(500, {"error": {"type": type(error).__name__, "message": str(error)}})

        def log_message(self, format: str, *args: Any) -> None:
            print("%s - %s" % (self.address_string(), format % args), flush=True)

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8002)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--batch-window-ms", type=int, default=100)
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1")
    backend = GemmaBackend(args.model, batch_size=args.batch_size, batch_window_ms=args.batch_window_ms)
    print(f"Gemma adapter ready at http://{args.host}:{args.port}/v1", flush=True)
    ThreadingHTTPServer((args.host, args.port), handler_for(backend)).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
