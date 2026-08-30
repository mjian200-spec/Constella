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


PROMPT_FILES = {
    "concept_merge": "concept_merge_v1.yaml",
    "concept_merge_review": "concept_merge_review_v1.yaml",
    "object_alignment": "object_alignment_v1.yaml",
    "state_object_alignment": "state_object_alignment_v1.yaml",
    "state_repair": "state_repair_v1.yaml",
    "state_normalization": "state_normalization_v1.yaml",
}


class SemanticAlignmentRunner:
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
        self.prompt_dir = Path(prompt_dir)
        self.output_dir = Path(output_dir)
        self.workers = workers
        self.client = client or LLMClient(models)
        self.prompts = {
            package_type: self._load_prompt(self.prompt_dir / filename)
            for package_type, filename in PROMPT_FILES.items()
        }

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
        refresh: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        selected = packages[:limit] if limit is not None else packages
        started = time.monotonic()
        results: list[dict[str, Any] | None] = [None] * len(selected)
        cached_count = 0
        pending: list[tuple[int, dict[str, Any]]] = []
        for index, package in enumerate(selected):
            cached = None if refresh else self._load_cached(package)
            if cached is None:
                pending.append((index, package))
            else:
                results[index] = cached
                cached_count += 1
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
                        "status": "failed",
                        "attempt_count": 0,
                        "covered_item_count": 0,
                        "errors": [f"unhandled:{type(error).__name__}: {error}"],
                    }
        final = [row for row in results if row is not None]
        success = [row for row in final if row["status"] == "success"]
        report = {
            "package_type": selected[0]["package_type"] if selected else None,
            "selected_package_count": len(selected),
            "success_count": len(success),
            "failed_count": len(final) - len(success),
            "cached_count": cached_count,
            "protocol_success_rate": round(len(success) / len(selected), 4) if selected else 1.0,
            "decision_coverage_rate": round(
                sum(row.get("covered_item_count", 0) for row in success)
                / max(1, sum(self._item_count(package) for package in selected)),
                4,
            ),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        if report["package_type"] == "state_normalization":
            group_count = sum(row.get("quality", {}).get("state_group_count", 0) for row in success)
            explicit_count = sum(
                row.get("quality", {}).get("explicit_subject_group_count", 0) for row in success
            )
            report["state_group_count"] = group_count
            report["explicit_subject_group_count"] = explicit_count
            report["explicit_subject_rate"] = round(explicit_count / group_count, 4) if group_count else 1.0
        return final, report

    def _process(self, package: dict[str, Any]) -> dict[str, Any]:
        package_type = str(package["package_type"])
        prompt = self.prompts[package_type]
        fingerprint = self._fingerprint(package, prompt)
        messages = [
            {"role": "system", "content": prompt["system"]},
            {"role": "user", "content": json.dumps(package, ensure_ascii=False)},
        ]
        errors: list[str] = []
        raw_outputs: list[str] = []
        for attempt in range(1, 3):
            try:
                response = self.client.complete(
                    self.model_key,
                    messages,
                    response_format={"type": "json_object"},
                    prompt_id=prompt["id"],
                    prompt_version=str(prompt["version"]),
                    input_unit_ids=[],
                    max_tokens=int(prompt.get("max_tokens", 800)),
                )
                content = response["choices"][0]["message"]["content"]
                raw_outputs.append(content)
                value = json.loads(content)
                value = self._normalize_output(package, value)
                covered = self._validate(package, value)
                result = {
                    "package_id": package["package_id"],
                    "package_type": package_type,
                    "status": "success",
                    "attempt_count": attempt,
                    "input_fingerprint": fingerprint,
                    "covered_item_count": covered,
                    "output": value,
                }
                quality = self._quality(package, value)
                if quality:
                    result["quality"] = quality
                try:
                    self._store(package, result)
                except Exception as error:
                    result["cache_write_error"] = f"{type(error).__name__}: {error}"
                return result
            except Exception as error:
                errors.append(f"{type(error).__name__}: {error}")
                if attempt == 1:
                    messages.append({
                        "role": "user",
                        "content": f"输出不符合协议：{error}。只返回修正后的JSON，不要解释。",
                    })
        result = {
            "package_id": package["package_id"],
            "package_type": package_type,
            "status": "failed",
            "attempt_count": 2,
            "input_fingerprint": fingerprint,
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
    def _normalize_output(package: dict[str, Any], value: Any) -> Any:
        if package.get("package_type") != "state_repair" or not isinstance(value, dict):
            return value
        repairs = value.get("repairs")
        if not isinstance(repairs, list):
            return value
        repairs = [
            row for row in repairs
            if isinstance(row, dict) and isinstance(row.get("parts"), list) and row["parts"]
        ]
        value["repairs"] = repairs
        repaired_ids = {row.get("state_id") for row in repairs if isinstance(row, dict)}
        unresolved = value.get("unresolved_ids")
        invalid = value.get("invalid_ids")
        if isinstance(unresolved, list):
            value["unresolved_ids"] = list(dict.fromkeys(
                state_id for state_id in unresolved if state_id not in repaired_ids
            ))
        terminal_ids = repaired_ids | set(value.get("unresolved_ids") or [])
        if isinstance(invalid, list):
            value["invalid_ids"] = list(dict.fromkeys(
                state_id for state_id in invalid if state_id not in terminal_ids
            ))
        return value

    @staticmethod
    def _quality(package: dict[str, Any], value: dict[str, Any]) -> dict[str, int] | None:
        if package.get("package_type") != "state_normalization":
            return None
        names = [package["concept"].get("name"), *(package["concept"].get("aliases") or [])]
        normalized_names = {"".join(str(name).split()).lower() for name in names if name}
        groups = value.get("groups") or []
        explicit = sum(
            any(name in "".join(str(group.get("canonical") or "").split()).lower() for name in normalized_names)
            for group in groups
        )
        return {"state_group_count": len(groups), "explicit_subject_group_count": explicit}

    def _validate(self, package: dict[str, Any], value: dict[str, Any]) -> int:
        if not isinstance(value, dict):
            raise ValueError("output must be an object")
        package_type = package["package_type"]
        if package_type == "concept_merge":
            return self._validate_concept_merge(package, value)
        if package_type == "concept_merge_review":
            return self._validate_concept_merge_review(package, value)
        if package_type == "object_alignment":
            return self._validate_object_alignment(package, value)
        if package_type == "state_object_alignment":
            return self._validate_state_object_alignment(package, value)
        if package_type == "state_repair":
            return self._validate_state_repair(package, value)
        if package_type == "state_normalization":
            return self._validate_state_normalization(package, value)
        raise ValueError(f"Unsupported package type: {package_type}")

    @staticmethod
    def _validate_concept_merge(package: dict[str, Any], value: dict[str, Any]) -> int:
        groups = value.get("merge_groups")
        if not isinstance(groups, list):
            raise ValueError("merge_groups must be a list")
        allowed: set[str] = set()
        for case in package["cases"]:
            allowed.add(case["anchor"]["id"])
            allowed.update(item["id"] for item in case["candidates"])
        seen: set[str] = set()
        for group in groups:
            if not isinstance(group, list) or len(group) < 2 or not all(isinstance(item, str) for item in group):
                raise ValueError("every merge group requires at least two concept ids")
            if not set(group) <= allowed:
                raise ValueError("merge group contains an unknown concept id")
            if len(set(group)) != len(group) or seen & set(group):
                raise ValueError("concept ids must not repeat across merge groups")
            seen.update(group)
        return len(package["cases"])

    @staticmethod
    def _validate_object_alignment(package: dict[str, Any], value: dict[str, Any]) -> int:
        rows = value.get("alignments")
        if not isinstance(rows, list):
            raise ValueError("alignments must be a list")
        cases = {item["object_id"]: item for item in package["cases"]}
        package_concept_ids = {
            candidate["id"] for case in package["cases"] for candidate in case["candidates"]
        }
        if {item.get("object_id") for item in rows} != set(cases):
            raise ValueError("alignments must cover every object exactly once")
        if len(rows) != len(cases):
            raise ValueError("duplicate object alignment")
        for row in rows:
            allowed = package_concept_ids | {"NEW", "REPARSE", "INVALID"}
            if row.get("concept_id") not in allowed:
                raise ValueError("concept_id must be a candidate id, NEW, REPARSE, or INVALID")
        return len(cases)

    @staticmethod
    def _validate_concept_merge_review(package: dict[str, Any], value: dict[str, Any]) -> int:
        pairs = value.get("merge_pairs")
        if not isinstance(pairs, list):
            raise ValueError("merge_pairs must be a list")
        allowed = {
            tuple(sorted((case["left"]["id"], case["right"]["id"])))
            for case in package["cases"]
        }
        normalized: list[tuple[str, str]] = []
        for pair in pairs:
            if not isinstance(pair, list) or len(pair) != 2 or not all(isinstance(item, str) for item in pair):
                raise ValueError("every merge pair requires two concept ids")
            normalized.append(tuple(sorted(pair)))
        if len(normalized) != len(set(normalized)) or not set(normalized) <= allowed:
            raise ValueError("merge_pairs must be unique proposed pairs")
        return len(package["cases"])

    @staticmethod
    def _validate_state_object_alignment(package: dict[str, Any], value: dict[str, Any]) -> int:
        rows = value.get("alignments")
        if not isinstance(rows, list):
            raise ValueError("alignments must be a list")
        cases = {item["state_id"]: item for item in package["cases"]}
        if len(rows) != len(cases) or {item.get("state_id") for item in rows} != set(cases):
            raise ValueError("alignments must cover every state exactly once")
        package_concept_ids = {
            candidate["id"] for case in package["cases"] for candidate in case["candidates"]
        }
        allowed = package_concept_ids | {"INVALID", "UNRESOLVED"}
        if any(row.get("concept_id") not in allowed for row in rows):
            raise ValueError("concept_id must be a package candidate, INVALID, or UNRESOLVED")
        return len(cases)

    @staticmethod
    def _validate_state_repair(package: dict[str, Any], value: dict[str, Any]) -> int:
        decisions = value.get("repairs")
        unresolved = value.get("unresolved_ids")
        invalid = value.get("invalid_ids")
        if not all(isinstance(rows, list) for rows in (decisions, unresolved, invalid)):
            raise ValueError("repairs, unresolved_ids, and invalid_ids must be lists")
        cases = {item["state_id"]: item for item in package["cases"]}
        repaired_ids = [row.get("state_id") for row in decisions]
        covered = [*repaired_ids, *unresolved, *invalid]
        if len(covered) != len(set(covered)) or set(covered) != set(cases):
            missing = sorted(set(cases) - set(covered))
            unexpected = sorted(set(covered) - set(cases))
            duplicates = sorted(state_id for state_id in set(covered) if covered.count(state_id) > 1)
            raise ValueError(
                "repair output must cover every state exactly once; "
                f"missing={missing}, unexpected={unexpected}, duplicates={duplicates}"
            )
        allowed = {
            candidate["id"] for case in package["cases"] for candidate in case["candidates"]
        } | {"NEW"}
        for decision in decisions:
            parts = decision.get("parts")
            if not isinstance(parts, list) or not 1 <= len(parts) <= 8:
                raise ValueError("every repair requires between one and eight atomic parts")
            seen_parts: set[tuple[str, str, str]] = set()
            for part in parts:
                if part.get("concept_id") not in allowed:
                    raise ValueError("repair concept_id must be a package candidate or NEW")
                object_name = part.get("object_name")
                state_text = part.get("state_text")
                if not isinstance(object_name, str) or not object_name.strip():
                    raise ValueError("every repair part requires object_name")
                if not isinstance(state_text, str) or not state_text.strip():
                    raise ValueError("every repair part requires state_text")
                key = (part["concept_id"], object_name.strip(), state_text.strip())
                if key in seen_parts:
                    raise ValueError("duplicate repair part")
                seen_parts.add(key)
        return len(cases)

    @staticmethod
    def _validate_state_normalization(package: dict[str, Any], value: dict[str, Any]) -> int:
        groups = value.get("groups")
        exceptions = value.get("exceptions")
        if not isinstance(groups, list) or not isinstance(exceptions, list):
            raise ValueError("groups and exceptions must be lists")
        allowed = {item["id"] for item in package["states"]}
        covered: list[str] = []
        for group in groups:
            members = group.get("members")
            if not isinstance(group.get("canonical"), str) or not group["canonical"].strip():
                raise ValueError("every state group requires canonical text")
            if not isinstance(members, list) or not members:
                raise ValueError("every state group requires members")
            covered.extend(members)
        for row in exceptions:
            if row.get("type") not in {"WRONG_CONCEPT", "INVALID", "UNCERTAIN"}:
                raise ValueError("invalid state exception type")
            covered.append(row.get("state_id"))
        if len(covered) != len(set(covered)) or set(covered) != allowed:
            raise ValueError("groups and exceptions must cover every state exactly once")
        return len(allowed)

    def _load_cached(self, package: dict[str, Any]) -> dict[str, Any] | None:
        path = self._result_path(package)
        if not path.is_file():
            return None
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
            prompt = self.prompts[package["package_type"]]
            if result.get("input_fingerprint") != self._fingerprint(package, prompt):
                return None
            if result.get("status") != "success":
                return None
            self._validate(package, result["output"])
            return result
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _store(self, package: dict[str, Any], result: dict[str, Any]) -> None:
        package_path = self._package_path(package)
        result_path = self._result_path(package)
        self._atomic_json(package_path, package)
        self._atomic_json(result_path, result)

    def _package_path(self, package: dict[str, Any]) -> Path:
        return self.output_dir / "packages" / package["package_type"] / f"{package['package_id']}.json"

    def _result_path(self, package: dict[str, Any]) -> Path:
        return self.output_dir / "results" / package["package_type"] / f"{package['package_id']}.json"

    @staticmethod
    def _atomic_json(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)

    @staticmethod
    def _fingerprint(package: dict[str, Any], prompt: dict[str, Any]) -> str:
        value = {
            "package": package,
            "prompt_id": prompt["id"],
            "prompt_version": str(prompt["version"]),
        }
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    @staticmethod
    def _item_count(package: dict[str, Any]) -> int:
        return len(package.get("cases") or package.get("states") or [])
