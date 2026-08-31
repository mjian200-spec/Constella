from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any

import yaml

from constella.context_builder.llm_client import LLMClient

from .models import PackageTier, SemanticRole, TIER_ORDER


class SemanticAlignmentRunner:
    """Run tier-homogeneous packages in confidence order against one prompt."""

    def __init__(
        self,
        models: dict[str, Any],
        model_key: str,
        prompt_dir: str | Path,
        output_dir: str | Path,
        *,
        workers: int = 1,
        client=None,
    ) -> None:
        if workers < 1:
            raise ValueError("workers must be at least 1")
        self.models = models
        self.model_key = model_key
        self.output_dir = Path(output_dir)
        self.workers = workers
        self.client = client or LLMClient(models)
        self.prompt = self._load_prompt(Path(prompt_dir) / "object_alignment_v1.yaml")

    @staticmethod
    def _load_prompt(path: Path) -> dict[str, Any]:
        prompt = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(prompt, dict) or not {"id", "version", "system"} <= set(prompt):
            raise ValueError(f"Invalid prompt: {path}")
        return prompt

    def run(
        self,
        packages: list[dict[str, Any]],
        *,
        limit: int | None = None,
        max_tier: str = PackageTier.H3,
        refresh: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if max_tier not in TIER_ORDER:
            raise ValueError(f"unsupported max tier: {max_tier}")
        eligible = [
            package for package in packages
            if TIER_ORDER[PackageTier(package["tier"])] <= TIER_ORDER[PackageTier(max_tier)]
        ]
        selected = eligible[:limit] if limit is not None else eligible
        started = time.monotonic()
        indexed = list(enumerate(selected))
        results: list[dict[str, Any] | None] = [None] * len(selected)
        cached_count = 0
        tier_reports: list[dict[str, Any]] = []
        for tier in sorted(
            {str(package["tier"]) for package in selected},
            key=lambda value: TIER_ORDER[PackageTier(value)],
        ):
            tier_started = time.monotonic()
            pending: list[tuple[int, dict[str, Any]]] = []
            tier_cached = 0
            for index, package in indexed:
                if str(package["tier"]) != tier:
                    continue
                cached = None if refresh else self._load_cached(package)
                if cached is None:
                    pending.append((index, package))
                else:
                    results[index] = cached
                    cached_count += 1
                    tier_cached += 1
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                futures = {pool.submit(self._process, package): index for index, package in pending}
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        results[index] = future.result()
                    except Exception as error:
                        package = selected[index]
                        results[index] = {
                            "package_id": package.get("package_id"),
                            "package_type": package.get("package_type"),
                            "tier": package.get("tier"),
                            "memory_version": package.get("memory_version"),
                            "status": "failed",
                            "attempt_count": 0,
                            "covered_item_count": 0,
                            "errors": [f"unhandled:{type(error).__name__}: {error}"],
                        }
            tier_results = [
                results[index] for index, package in indexed
                if str(package["tier"]) == tier and results[index] is not None
            ]
            tier_reports.append({
                "tier": tier,
                "package_count": len(tier_results),
                "success_count": sum(row["status"] == "success" for row in tier_results),
                "cached_count": tier_cached,
                "elapsed_seconds": round(time.monotonic() - tier_started, 3),
            })
        final = [row for row in results if row is not None]
        success = [row for row in final if row["status"] == "success"]
        report = {
            "package_type": "object_alignment",
            "selected_package_count": len(selected),
            "eligible_package_count": len(eligible),
            "success_count": len(success),
            "failed_count": len(final) - len(success),
            "cached_count": cached_count,
            "protocol_success_rate": round(len(success) / len(selected), 4) if selected else 1.0,
            "decision_coverage_rate": round(
                sum(row.get("covered_item_count", 0) for row in success)
                / max(1, sum(len(package["cases"]) for package in selected)),
                4,
            ),
            "max_tier": str(max_tier),
            "tier_reports": tier_reports,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        return final, report

    def _process(self, package: dict[str, Any]) -> dict[str, Any]:
        prompt_fingerprint = self._prompt_fingerprint(self.prompt)
        messages = [
            {"role": "system", "content": self.prompt["system"]},
            {"role": "user", "content": json.dumps(package, ensure_ascii=False)},
        ]
        errors: list[str] = []
        raw_outputs: list[str] = []
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            content: str | None = None
            try:
                response = self.client.complete(
                    self.model_key,
                    messages,
                    response_format={"type": "json_object"},
                    prompt_id=self.prompt["id"],
                    prompt_version=str(self.prompt["version"]),
                    input_unit_ids=[],
                    max_tokens=int(self.prompt.get("max_tokens", 2400)),
                )
                content = response["choices"][0]["message"]["content"]
                raw_outputs.append(content)
                value = json.loads(content)
                covered = self._validate(package, value)
                result = {
                    "package_id": package["package_id"],
                    "package_type": package["package_type"],
                    "tier": package["tier"],
                    "memory_version": package["memory_version"],
                    "status": "success",
                    "attempt_count": attempt,
                    "prompt_fingerprint": prompt_fingerprint,
                    "covered_item_count": covered,
                    "output": value,
                }
                try:
                    self._store(package, result)
                except Exception as error:
                    result["cache_write_error"] = f"{type(error).__name__}: {error}"
                return result
            except Exception as error:
                errors.append(f"{type(error).__name__}: {error}")
                if attempt < max_attempts:
                    if content is not None:
                        messages.append({"role": "assistant", "content": content})
                    correction = (
                        "若错误涉及long-tail：该对象只能使用DECOMPOSED或EXPRESSION_ONLY。"
                        "DECOMPOSED必须引用candidate_concepts中的已入库上层概念ID，"
                        "并在states或qualifiers中保留原对象相对上层概念的具体差异；"
                        "无法可靠满足时改用EXPRESSION_ONLY，禁止提出新的原子对象概念。"
                    )
                    messages.append({
                        "role": "user",
                        "content": f"输出不符合协议：{error}。{correction}只返回修正后的完整JSON，不要解释。",
                    })
        result = {
            "package_id": package["package_id"],
            "package_type": package["package_type"],
            "tier": package["tier"],
            "memory_version": package["memory_version"],
            "status": "failed",
            "attempt_count": max_attempts,
            "prompt_fingerprint": prompt_fingerprint,
            "covered_item_count": 0,
            "errors": errors,
            "raw_outputs": raw_outputs,
        }
        try:
            self._store(package, result)
        except Exception as error:
            result["cache_write_error"] = f"{type(error).__name__}: {error}"
        return result

    @staticmethod
    def _validate(package: dict[str, Any], value: dict[str, Any]) -> int:
        if not isinstance(value, dict):
            raise ValueError("output must be an object")
        rows = value.get("interpretations")
        if not isinstance(rows, list):
            raise ValueError("interpretations must be a list")
        cases = {str(case["object_id"]): case for case in package["cases"]}
        ids = [str(row.get("object_id") or "") for row in rows if isinstance(row, dict)]
        if len(ids) != len(set(ids)) or set(ids) != set(cases):
            raise ValueError("interpretations must cover every object exactly once")
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("every interpretation must be an object")
            decision = row.get("decision")
            if decision not in {"ATOMIC", "DECOMPOSED", "EXPRESSION_ONLY"}:
                raise ValueError("invalid interpretation decision")
            core_objects = row.get("core_objects")
            embedded_states = row.get("embedded_states")
            qualifiers = row.get("qualifiers")
            if not isinstance(core_objects, list) or not isinstance(embedded_states, list) or not isinstance(qualifiers, list):
                raise ValueError("core_objects, embedded_states, and qualifiers must be lists")
            if decision == "ATOMIC" and len(core_objects) != 1:
                raise ValueError("ATOMIC interpretation requires exactly one core object")
            if decision == "DECOMPOSED" and not 1 <= len(core_objects) <= 4:
                raise ValueError("DECOMPOSED interpretation requires one to four core objects")
            if decision == "EXPRESSION_ONLY" and core_objects:
                raise ValueError("EXPRESSION_ONLY must not invent core objects")
            allowed = {str(candidate["id"]) for candidate in cases[row["object_id"]]["candidates"]}
            seen_core: set[tuple[str, str]] = set()
            for core in core_objects:
                if not isinstance(core, dict) or not isinstance(core.get("text"), str) or not core["text"].strip():
                    raise ValueError("every core object requires non-empty text")
                concept_id = core.get("concept_id")
                if concept_id is not None and concept_id not in allowed:
                    raise ValueError(
                        f"object {row['object_id']} core concept_id {concept_id} is not in "
                        f"this case candidates {sorted(allowed)}; use null when none matches"
                    )
                key = (core["text"].strip(), str(concept_id or ""))
                if key in seen_core:
                    raise ValueError("duplicate core object")
                seen_core.add(key)
            if cases[row["object_id"]].get("long_tail_fallback_required"):
                if decision not in {"DECOMPOSED", "EXPRESSION_ONLY"}:
                    raise ValueError(
                        "long-tail object must use an upper-concept decomposition or expression-only fallback"
                    )
                if decision == "DECOMPOSED":
                    if not any(core.get("concept_id") for core in core_objects):
                        raise ValueError("long-tail decomposition must reference an approved upper concept")
                    if not embedded_states and not qualifiers:
                        raise ValueError("long-tail decomposition must preserve the specific difference as state or qualifier")
            if len(embedded_states) > 8:
                raise ValueError("an interpretation supports at most eight embedded states")
            for state in embedded_states:
                if not isinstance(state, dict) or state.get("role") not in {
                    SemanticRole.OBJECT_INTRINSIC_STATE, SemanticRole.RULE_CONDITION,
                }:
                    raise ValueError("invalid embedded state role")
                for field in ("subject_text", "state_text"):
                    if not isinstance(state.get(field), str) or not state[field].strip():
                        raise ValueError(f"embedded state requires {field}")
            if len(qualifiers) > 8 or not all(isinstance(item, str) and item.strip() for item in qualifiers):
                raise ValueError("qualifiers must contain at most eight non-empty strings")
        return len(cases)

    def _load_cached(self, package: dict[str, Any]) -> dict[str, Any] | None:
        path = self._result_path(package)
        if not path.is_file():
            return None
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
            if result.get("status") != "success":
                return None
            if result.get("prompt_fingerprint") is not None:
                # New-format cache: package_id is a content hash of the package,
                # so comparing it plus the prompt hash skips re-serializing the
                # whole package payload.
                if result.get("package_id") != package["package_id"]:
                    return None
                compatible = {
                    str(value)
                    for value in self.prompt.get("compatible_prompt_fingerprints") or []
                }
                if result.get("prompt_fingerprint") not in {
                    self._prompt_fingerprint(self.prompt), *compatible,
                }:
                    return None
            elif result.get("input_fingerprint") != self._fingerprint(package, self.prompt):
                return None
            self._validate(package, result["output"])
            return result
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None

    def _store(self, package: dict[str, Any], result: dict[str, Any]) -> None:
        self._atomic_json(self._package_path(package), package)
        self._atomic_json(self._result_path(package), result)

    def _package_path(self, package: dict[str, Any]) -> Path:
        return self.output_dir / "packages" / "object_alignment" / f"{package['package_id']}.json"

    def _result_path(self, package: dict[str, Any]) -> Path:
        return self.output_dir / "results" / "object_alignment" / f"{package['package_id']}.json"

    @staticmethod
    def _atomic_json(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _fingerprint(package: dict[str, Any], prompt: dict[str, Any]) -> str:
        payload = {"package": package, "prompt": prompt}
        return hashlib.sha256(json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()

    @staticmethod
    def _prompt_fingerprint(prompt: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(
            prompt, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
