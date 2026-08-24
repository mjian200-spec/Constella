from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import yaml

from constella.context_builder.llm_client import LLMClient

from .message_builder import MultimodalMessageBuilder
from .models import ResolvedContextPackage
from .reflection_patch import addressed_draft


class ModelOutputError(ValueError):
    pass


def load_prompt(path: str | Path) -> dict[str, Any]:
    prompt = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(prompt, dict) or not {"id", "version", "system"}.issubset(prompt):
        raise ValueError(f"Invalid rule extraction prompt: {path}")
    examples = prompt.get("examples", [])
    if examples:
        if not isinstance(examples, list) or not all(isinstance(item, dict) and {"input", "output"}.issubset(item) for item in examples):
            raise ValueError(f"Prompt examples must contain input/output pairs: {path}")
        rendered = "\n\n".join(
            f"示例 {index}\n上下文：{item['input']}\n合格输出：\n{item['output']}"
            for index, item in enumerate(examples, start=1)
        )
        prompt = dict(prompt)
        prompt["system"] = f"{prompt['system']}\n\n以下是必须模仿的少样本格式：\n{rendered}"
    return prompt


def response_content(response: dict[str, Any]) -> str:
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise ModelOutputError("Model response has no message content") from error
    if not isinstance(content, str) or not content.strip():
        raise ModelOutputError("Model response content is empty or not text")
    return content


class RuleGenerator:
    def __init__(self, models: dict[str, Any], model_key: str, generator_prompt: dict[str, Any], reflector_prompt: dict[str, Any], event_sink=None, call_sink=None, output_sink=None) -> None:
        self.client = LLMClient(models, event_sink=event_sink)
        self.models = models
        self.model_key = model_key
        self.generator_prompt = generator_prompt
        self.reflector_prompt = reflector_prompt
        self.builder = MultimodalMessageBuilder()
        self.call_sink = call_sink or (lambda **_: None)
        self.output_sink = output_sink or (lambda **_: None)

    @property
    def model_name(self) -> str:
        return str(self.models[self.model_key]["model"])

    def generate(self, package: ResolvedContextPackage) -> tuple[str, str]:
        context = self.builder.context_content(package)
        draft = self.generate_draft_from_context(context, package.id)
        final = self.reflect_from_context(context, package.id, draft)
        return draft, final

    def generate_draft_from_context(self, context: list[dict[str, Any]], package_id: str) -> str:
        draft = self._call(self.generator_prompt, context, package_id, phase="generate")
        self.output_sink(
            package_id=package_id, phase="generate", output=draft,
            prompt_id=self.generator_prompt["id"], prompt_version=str(self.generator_prompt["version"]),
        )
        return draft

    def reflect_from_context(self, context: list[dict[str, Any]], package_id: str, draft: str) -> str:
        reflection_blocks = list(context) + [{"type": "text", "text": (
            "以下是第一次抽取的 DSL 初稿。你是这份初稿的编辑器，不得重新独立生成另一份规则。"
            "正确内容不输出，只对错误或遗漏位置输出系统规定的稀疏补丁。\n"
            "原始生成初稿（每条R前的[G/R]只用于补丁定位，不属于DSL）：\n" + addressed_draft(draft)
        )}]
        final = self._call(self.reflector_prompt, reflection_blocks, package_id, phase="reflect")
        self.output_sink(
            package_id=package_id, phase="reflect", output=final,
            prompt_id=self.reflector_prompt["id"], prompt_version=str(self.reflector_prompt["version"]),
        )
        return final

    def _call(self, prompt: dict[str, Any], content: list[dict[str, Any]], package_id: str, *, phase: str) -> str:
        messages = [{"role": "system", "content": prompt["system"]}, {"role": "user", "content": content}]
        retries = int(self.models[self.model_key].get("max_retries", 2))
        for attempt in range(retries + 1):
            started = time.monotonic()
            try:
                response = self.client.complete(
                    self.model_key, messages, prompt_id=prompt["id"], prompt_version=prompt["version"],
                    input_unit_ids=[package_id], max_tokens=self.models[self.model_key].get("max_tokens"),
                )
                output = response_content(response)
                self.call_sink(phase=phase, package_id=package_id, model=self.model_name, prompt_id=prompt["id"],
                               prompt_version=str(prompt["version"]), status="ok", latency_seconds=time.monotonic() - started,
                               input_text_chars=sum(len(str(block.get("text", ""))) for block in content),
                               image_count=sum(block.get("type") == "image_url" for block in content), output_chars=len(output))
                return output
            except Exception as error:
                self.call_sink(phase=phase, package_id=package_id, model=self.model_name, prompt_id=prompt["id"],
                               prompt_version=str(prompt["version"]), status="error", latency_seconds=time.monotonic() - started,
                               input_text_chars=sum(len(str(block.get("text", ""))) for block in content),
                               image_count=sum(block.get("type") == "image_url" for block in content), error_type=type(error).__name__)
                if attempt >= retries:
                    raise
                time.sleep(0.5 * (attempt + 1))
