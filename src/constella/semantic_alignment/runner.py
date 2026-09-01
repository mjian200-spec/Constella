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
                if (
                    content is not None
                    and len(package.get("cases") or []) > 1
                    and (
                        "is not in this case candidates" in str(error)
                        or "cover every object exactly once" in str(error)
                    )
                ):
                    break
                if attempt < max_attempts:
                    if content is not None:
                        messages.append({"role": "assistant", "content": content})
                    correction = (
                        "完整对象等价于候选时用BIND；需要改写为已有对象并迁移对象短语中的"
                        "条件或状态时用REWRITE；稳定领域对象没有匹配时用CREATE；公式、符号、"
                        "元话语或无意义抽取用DISCARD。不得输出核、修饰属性或未定义的兜底类别。"
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
        if len(package.get("cases") or []) > 1:
            split_results = [
                self._process({
                    **package,
                    "package_id": f"{package['package_id']}__{case['object_id']}",
                    "parent_package_id": package["package_id"],
                    "cases": [case],
                })
                for case in package["cases"]
            ]
            if all(row.get("status") == "success" for row in split_results):
                result = {
                    "package_id": package["package_id"],
                    "package_type": package["package_type"],
                    "tier": package["tier"],
                    "memory_version": package["memory_version"],
                    "status": "success",
                    "attempt_count": max_attempts,
                    "split_attempt_count": sum(int(row.get("attempt_count") or 0) for row in split_results),
                    "fallback_mode": "split_cases",
                    "prompt_fingerprint": prompt_fingerprint,
                    "covered_item_count": len(package["cases"]),
                    "output": {
                        "interpretations": [
                            interpretation
                            for row in split_results
                            for interpretation in row["output"]["interpretations"]
                        ],
                    },
                }
            else:
                result["split_failures"] = [
                    {
                        "package_id": row.get("package_id"),
                        "errors": row.get("errors") or [],
                    }
                    for row in split_results if row.get("status") != "success"
                ]
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
            if decision not in {"BIND", "REWRITE", "CREATE", "DISCARD"}:
                raise ValueError("invalid interpretation decision")
            normalized_objects = row.get("normalized_objects")
            derived_states = row.get("derived_states")
            proposal = row.get("proposal")
            discard = row.get("discard")
            if not isinstance(normalized_objects, list) or not isinstance(derived_states, list):
                raise ValueError("normalized_objects and derived_states must be lists")
            if decision == "BIND" and len(normalized_objects) != 1:
                raise ValueError("BIND requires exactly one normalized object")
            if decision == "REWRITE" and not 1 <= len(normalized_objects) <= 4:
                raise ValueError("REWRITE requires one to four normalized objects")
            if decision == "CREATE" and len(normalized_objects) != 1:
                raise ValueError("CREATE requires exactly one proposed normalized object")
            if decision == "DISCARD" and (normalized_objects or derived_states):
                raise ValueError("DISCARD must not emit normalized objects or derived states")
            allowed = {str(candidate["id"]) for candidate in cases[row["object_id"]]["candidates"]}
            seen_objects: set[tuple[str, str]] = set()
            for normalized in normalized_objects:
                if not isinstance(normalized, dict) or not isinstance(normalized.get("text"), str) or not normalized["text"].strip():
                    raise ValueError("every normalized object requires non-empty text")
                concept_id = normalized.get("concept_id")
                if concept_id is not None and concept_id not in allowed:
                    raise ValueError(
                        f"object {row['object_id']} normalized concept_id {concept_id} is not in "
                        f"this case candidates {sorted(allowed)}"
                    )
                if decision in {"BIND", "REWRITE"} and concept_id is None:
                    raise ValueError(f"{decision} normalized objects must reference supplied concepts")
                if decision == "CREATE" and concept_id is not None:
                    raise ValueError("CREATE must not reference an existing concept")
                key = (normalized["text"].strip(), str(concept_id or ""))
                if key in seen_objects:
                    raise ValueError("duplicate normalized object")
                seen_objects.add(key)
            if decision == "REWRITE" and len(normalized_objects) == 1 and not derived_states:
                raise ValueError("single-object REWRITE must preserve the rewritten difference as a derived state")
            if len(derived_states) > 8:
                raise ValueError("an interpretation supports at most eight derived states")
            normalized_texts = {item["text"].strip() for item in normalized_objects}
            for state in derived_states:
                if not isinstance(state, dict) or state.get("role") not in {
                    SemanticRole.OBJECT_INTRINSIC_STATE, SemanticRole.RULE_CONDITION,
                }:
                    raise ValueError("invalid derived state role")
                for field in ("subject_text", "state_text"):
                    if not isinstance(state.get(field), str) or not state[field].strip():
                        raise ValueError(f"derived state requires {field}")
                if state["subject_text"].strip() not in normalized_texts:
                    raise ValueError("derived state subject must be one of the normalized objects")
            if decision == "CREATE":
                if not isinstance(proposal, dict):
                    raise ValueError("CREATE requires a proposal")
                if str(proposal.get("canonical_name") or "").strip() != normalized_objects[0]["text"].strip():
                    raise ValueError("proposal canonical_name must equal the proposed normalized object")
                relation_hints = proposal.get("relation_hints")
                if not isinstance(relation_hints, list) or len(relation_hints) > 8:
                    raise ValueError("proposal relation_hints must be a list of at most eight items")
                for hint in relation_hints:
                    if not isinstance(hint, dict) or hint.get("type") not in {"IS_A", "PART_OF", "PROPERTY_OF"}:
                        raise ValueError("invalid proposal relation hint type")
                    if hint.get("direction") not in {"OUTGOING", "INCOMING"}:
                        raise ValueError("invalid proposal relation hint direction")
                    if str(hint.get("target_concept_id") or "") not in allowed:
                        raise ValueError("proposal relation hint target must be a supplied candidate")
                if discard is not None:
                    raise ValueError("CREATE discard must be null")
            elif proposal is not None:
                raise ValueError("only CREATE may emit a proposal")
            if decision == "DISCARD":
                if not isinstance(discard, dict):
                    raise ValueError("DISCARD requires structured discard metadata")
                if discard.get("reason_code") not in {
                    "FORMULA_REFERENCE", "SYMBOL_OR_VARIABLE", "META_MENTION",
                    "NON_ENTITY_PREDICATE", "OCR_NOISE", "EMPTY_SEMANTICS",
                }:
                    raise ValueError("invalid discard reason_code")
                if not isinstance(discard.get("reason"), str) or not discard["reason"].strip():
                    raise ValueError("DISCARD requires a non-empty reason")
            elif discard is not None:
                raise ValueError("only DISCARD may emit discard metadata")
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
