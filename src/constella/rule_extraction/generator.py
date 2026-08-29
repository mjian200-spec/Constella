from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import yaml

from constella.context_builder.llm_client import LLMClient

from .message_builder import MultimodalMessageBuilder
from .reflection_patch import ReflectionPatchError, addressed_draft, apply_reflection_patch


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
    def __init__(self, models: dict[str, Any], model_key: str, reflector_prompt: dict[str, Any], repair_prompt: dict[str, Any], call_sink=None, output_sink=None) -> None:
        self.client = LLMClient(models)
        self.models = models
        self.model_key = model_key
        self.reflector_prompt = reflector_prompt
        self.repair_prompt = repair_prompt
        self.builder = MultimodalMessageBuilder()
        self.call_sink = call_sink or (lambda **_: None)
        self.output_sink = output_sink or (lambda **_: None)

    @property
    def model_name(self) -> str:
        return str(self.models[self.model_key]["model"])

    def generate_draft_from_context(
        self, context: list[dict[str, Any]], package_id: str, *, prompt: dict[str, Any],
    ) -> str:
        draft = self._call(prompt, context, package_id, phase="generate")
        self.output_sink(
            package_id=package_id, phase="generate", output=draft,
            prompt_id=prompt["id"], prompt_version=str(prompt["version"]),
        )
        return draft

    def reflect_from_context(self, context: list[dict[str, Any]], package_id: str, draft: str) -> tuple[str, str]:
        reflection_blocks = self._reflection_blocks(context, draft)
        patch = self._call(self.reflector_prompt, reflection_blocks, package_id, phase="reflect")
        return self.ensure_valid_reflection(context, package_id, draft, patch)

    @staticmethod
    def _reflection_blocks(context: list[dict[str, Any]], draft: str) -> list[dict[str, Any]]:
        try:
            editable_draft = addressed_draft(draft)
        except ReflectionPatchError as error:
            # The reflector is also responsible for recovering a generator
            # draft that violates the editable protocol.  Preserve the raw
            # draft as evidence and require the protocol's explicit whole-
            # draft repair command; do not silently normalize its semantics in
            # Python before the model has reviewed it.
            editable_draft = (
                "初稿不满足可寻址编辑协议，无法添加[G/R]地址。"
                "本次只能输出REPLACE_ALL...END_ALL，把它修成完整可解析DSL；不得解释。\n"
                f"协议错误：{error}\n"
                "原始未经改写初稿：\n" + draft
            )
        return list(context) + [{"type": "text", "text": (
            "以下是第一次抽取的 DSL 初稿。你是这份初稿的编辑器，不得重新独立生成另一份规则。"
            "正确内容不输出，只对错误或遗漏位置输出系统规定的稀疏补丁。\n"
            "原始生成初稿（每条R前的[G/R]只用于补丁定位，不属于DSL）：\n" + editable_draft
        )}]

    def ensure_valid_reflection(
        self, context: list[dict[str, Any]], package_id: str, draft: str, patch: str,
    ) -> tuple[str, str]:
        """Require the reflector—not the parser—to repair a malformed patch."""
        reflection_blocks = self._reflection_blocks(context, draft)
        for repair_attempt in range(3):
            try:
                candidate = apply_reflection_patch(draft, patch)
                break
            except ReflectionPatchError as error:
                if repair_attempt >= 2:
                    raise
                rejected_excerpt = patch if len(patch) <= 6000 else patch[:1000] + "\n...[中间省略]...\n" + patch[-5000:]
                correction = {
                    "type": "text",
                    "text": (
                        "你刚才的补丁无法执行。不要解释原因，只重新输出一份完整、最小、可执行的补丁。\n"
                        f"执行器错误：{error}\n"
                        "被拒绝的补丁：\n" + rejected_excerpt
                    ),
                }
                patch = self._call(
                    self.repair_prompt, reflection_blocks + [correction], package_id, phase="reflect_repair",
                )
        self.output_sink(
            package_id=package_id, phase="reflect", output=patch,
            prompt_id=self.reflector_prompt["id"], prompt_version=str(self.reflector_prompt["version"]),
        )
        return patch, candidate

    def _call(self, prompt: dict[str, Any], content: list[dict[str, Any]], package_id: str, *, phase: str) -> str:
        messages = [{"role": "system", "content": prompt["system"]}, {"role": "user", "content": content}]
        retries = int(self.models[self.model_key].get("max_retries", 2))
        input_text_chars = sum(len(str(block.get("text", ""))) for block in content)
        image_count = sum(block.get("type") == "image_url" for block in content)
        for attempt in range(retries + 1):
            started = time.monotonic()
            try:
                response = self.client.complete(
                    self.model_key, messages, prompt_id=prompt["id"], prompt_version=prompt["version"],
                    input_unit_ids=[package_id],
                    max_tokens=prompt.get("max_tokens", self.models[self.model_key].get("max_tokens")),
                )
                output = response_content(response)
                self.call_sink(phase=phase, package_id=package_id, model=self.model_name, prompt_id=prompt["id"],
                               prompt_version=str(prompt["version"]), status="ok", latency_seconds=time.monotonic() - started,
                               input_text_chars=input_text_chars, image_count=image_count, output_chars=len(output))
                return output
            except Exception as error:
                self.call_sink(phase=phase, package_id=package_id, model=self.model_name, prompt_id=prompt["id"],
                               prompt_version=str(prompt["version"]), status="error", latency_seconds=time.monotonic() - started,
                               input_text_chars=input_text_chars, image_count=image_count, error_type=type(error).__name__)
                if attempt >= retries:
                    raise
                time.sleep(0.5 * (attempt + 1))
