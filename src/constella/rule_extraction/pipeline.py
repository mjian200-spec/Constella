from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .context_cache import ContextCache
from .generator import RuleGenerator, load_prompt
from .graph_writer import Neo4jRuleGraphWriter
from .io import export_outputs, write_ruleset
from .parser import RuleParseError, parse_final_expression
from .prompt_router import RoutedPromptRegistry, route_modalities, route_name
from .resolver import DocumentGraphIndex, is_rule_extraction_package, iter_packages, resolve_package
from .models import PackageProcessingResult
from .state_store import StateStore


@dataclass(slots=True)
class RuleExtractionRuntime:
    config_dir: Path
    output_dir: Path
    model_key: str = "large"
    workers: int = 1
    asset_root: Path | None = None
    dry_run_resolve: bool = False
    no_graph: bool = False
    max_tokens: int | None = None
    refresh_model_output: bool = False
    show_progress: bool = False


class _TerminalProgress:
    def __init__(self, state_path: Path, run_id: str, package_ids: set[str]) -> None:
        self.state_path = state_path
        self.run_id = run_id
        self.package_ids = package_ids
        self.total = len(package_ids)
        self.started = time.monotonic()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="rule-progress", daemon=True)
        self.tty = sys.stderr.isatty()
        self.last_length = 0

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=2)
        self._render(final=True)

    def _run(self) -> None:
        while not self.stop_event.wait(1):
            self._render(final=False)

    def _counts(self) -> dict[str, int]:
        try:
            connection = sqlite3.connect(f"file:{self.state_path}?mode=ro", uri=True, timeout=5)
            rows = connection.execute(
                "SELECT context_package_id,status FROM package_states WHERE run_id=?", (self.run_id,),
            ).fetchall()
            connection.close()
            counts: dict[str, int] = {}
            for package_id, status in rows:
                if str(package_id) in self.package_ids:
                    counts[str(status)] = counts.get(str(status), 0) + 1
            return counts
        except sqlite3.Error:
            return {}

    @staticmethod
    def _duration(seconds: float | None) -> str:
        if seconds is None:
            return "--:--"
        seconds = max(0, int(seconds))
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"

    def _render(self, *, final: bool) -> None:
        counts = self._counts()
        success, no_rule, failed = (counts.get(name, 0) for name in ("success", "no_rule", "failed"))
        completed = success + no_rule + failed
        active = sum(counts.get(name, 0) for name in (
            "generating", "reflecting", "applying_reflection", "parsing", "persisting", "writing_graph",
        ))
        elapsed = max(time.monotonic() - self.started, 0.001)
        rate = completed / elapsed
        eta = (self.total - completed) / rate if rate > 0 else None
        ratio = min(1.0, completed / self.total) if self.total else 1.0
        width = 28
        filled = round(width * ratio)
        bar = "=" * filled + ">" * (filled < width) + "." * max(0, width - filled - (filled < width))
        phases = f"生成{counts.get('generating', 0)} 反思{counts.get('reflecting', 0)} 应用{counts.get('applying_reflection', 0)}"
        line = (
            f"[{bar}] {completed}/{self.total} {ratio * 100:5.1f}% 运行{active} "
            f"成功{success} 无规则{no_rule} 失败{failed} {phases} "
            f"{rate * 60:.1f}包/分 ETA {self._duration(eta)}"
        )
        if self.tty:
            padding = " " * max(0, self.last_length - len(line))
            print("\r" + line + padding, end="\n" if final else "", file=sys.stderr, flush=True)
            self.last_length = len(line)
        elif final:
            print(line, file=sys.stderr, flush=True)


def load_runtime(config_dir: str | Path, output_dir: str | Path, **kwargs: Any) -> RuleExtractionRuntime:
    return RuleExtractionRuntime(Path(config_dir), Path(output_dir), **kwargs)


def _load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a mapping in {path}")
    return value


def _input_fingerprint(graph_path: Path, packages_path: Path) -> str:
    digest = hashlib.sha256()
    for path in (graph_path, packages_path):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _failure(package_id: str, run_id: str, fingerprint: str, stage: str, code: str, error: Exception) -> PackageProcessingResult:
    return PackageProcessingResult(
        context_package_id=package_id, status="failed", failure_stage=stage, failure_code=code,
        failure_reason=str(error), input_fingerprint=fingerprint, run_id=run_id,
    )


def _model_output_path(output_dir: Path, package_id: str, phase: str) -> Path:
    return output_dir / "cache" / "model_outputs" / package_id / f"{phase}.json"


def _store_model_output(
    output_dir: Path, package_id: str, phase: str, fingerprint: str, output: str,
    prompt_id: str, prompt_version: str,
) -> None:
    path = _model_output_path(output_dir, package_id, phase)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "input_fingerprint": fingerprint,
        "prompt_id": prompt_id,
        "prompt_version": prompt_version,
        "output": output,
    }, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _load_model_output(
    output_dir: Path, package_id: str, phase: str, fingerprint: str, prompt: dict[str, Any],
) -> str | None:
    path = _model_output_path(output_dir, package_id, phase)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        valid = (
            raw.get("input_fingerprint") == fingerprint
            and raw.get("prompt_id") == prompt["id"]
            and raw.get("prompt_version") == str(prompt["version"])
            and isinstance(raw.get("output"), str)
        )
        return raw["output"] if valid else None
    except (OSError, TypeError, ValueError, KeyError):
        return None


def _resolve_with_cache(
    index: DocumentGraphIndex, cache: ContextCache, package: dict[str, Any],
):
    candidate = resolve_package(index, package)
    resolved = cache.load(package["id"], candidate.source_fingerprint)
    if resolved is not None:
        return resolved
    cache.write(candidate)
    return candidate


def run_rule_extraction(
    context_output_dir: str | Path, runtime: RuleExtractionRuntime, *, package_ids: set[str] | None = None,
    limit: int | None = None, resume: bool = False, retry_failed: bool = False, reset_graph: bool = False,
) -> dict[str, Any]:
    if runtime.workers < 1:
        raise ValueError("workers must be at least 1")
    started = time.monotonic()
    context_dir = Path(context_output_dir)
    graph_path, packages_path = context_dir / "document_graph.json", context_dir / "context_packages.jsonl"
    if not graph_path.is_file() or not packages_path.is_file():
        raise FileNotFoundError("Context Builder output requires document_graph.json and context_packages.jsonl")
    runtime.output_dir.mkdir(parents=True, exist_ok=True)
    fingerprint = _input_fingerprint(graph_path, packages_path)
    all_packages = [
        package for package in iter_packages(packages_path)
        if is_rule_extraction_package(package)
    ]
    packages = all_packages
    if package_ids:
        packages = [item for item in packages if item["id"] in package_ids]
    if limit is not None:
        packages = packages[:limit]
    index = DocumentGraphIndex.load(graph_path, runtime.asset_root)
    cache = ContextCache(runtime.output_dir / "cache" / "contexts")
    if runtime.dry_run_resolve:
        failures: list[dict[str, str]] = []
        for package in packages:
            try:
                _resolve_with_cache(index, cache, package)
            except Exception as error:
                failures.append({"context_package_id": package["id"], "reason": str(error)})
        report = {
            "run_id": "dry_run_resolve", "input_fingerprint": fingerprint,
            "selected_package_count": len(packages), "resolved_count": len(packages) - len(failures),
            "success_count": len(packages) - len(failures), "no_rule_count": 0,
            "failed_count": len(failures), "failures": failures,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        (runtime.output_dir / "rule_extraction_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        return report
    state = StateStore(runtime.output_dir / "rule_extraction_state.sqlite3")

    config = _load_yaml(runtime.config_dir / "models.yaml")
    models = config.get("models", {})
    if runtime.model_key not in models:
        raise ValueError(f"Unknown model key: {runtime.model_key}")
    models = {key: dict(value) for key, value in models.items()}
    if runtime.max_tokens is not None:
        models[runtime.model_key]["max_tokens"] = runtime.max_tokens
    model = str(models[runtime.model_key]["model"])
    routed_prompts = RoutedPromptRegistry(runtime.config_dir.parents[1] / "prompts" / "rule_extraction")
    reflector_prompt = load_prompt(
        runtime.config_dir.parents[1] / "prompts" / "rule_extraction" / "rule_reflector_full_v1.yaml"
    )
    repair_prompt = load_prompt(
        runtime.config_dir.parents[1] / "prompts" / "rule_extraction" / "rule_reflector_protocol_repair_v1.yaml"
    )
    active_prompt_versions = {
        "generator": routed_prompts.descriptor(),
        "reflector": reflector_prompt["version"],
        "reflection_repair": repair_prompt["version"],
    }

    latest = state.latest_run()
    if reset_graph and resume:
        raise ValueError("--reset-graph and --resume cannot be used together")
    if resume:
        if latest is None:
            raise ValueError("No previous extraction run exists to resume")
        if latest["input_fingerprint"] != fingerprint:
            raise ValueError("Input fingerprint changed; cannot safely resume this extraction run")
        previous_prompt_versions = json.loads(latest["prompt_versions"] or "{}")
        if previous_prompt_versions != active_prompt_versions:
            raise ValueError(
                "Prompt routing or prompt versions changed; cannot mix a previous extraction strategy into this run"
            )
        run_id = latest["run_id"]
    else:
        if latest is not None and not reset_graph:
            raise ValueError("An extraction run already exists; use --resume or explicitly confirm a new graph reset with --reset-graph")
        run_id = state.create_run(fingerprint, model, active_prompt_versions)

    neo4j_config = _load_yaml(runtime.config_dir / "neo4j.yaml").get("neo4j", {})
    password_env = neo4j_config.get("password_env", "CONSTELLA_NEO4J_PASSWORD")
    password = os.environ.get(password_env)
    if not runtime.no_graph and not password:
        raise ValueError(f"Neo4j password is required in environment variable {password_env}")
    writer = None
    if not runtime.no_graph:
        writer = Neo4jRuleGraphWriter(
            str(neo4j_config.get("uri", "bolt://127.0.0.1:7200")), str(neo4j_config.get("username", "neo4j")),
            password or "", str(neo4j_config.get("database", "neo4j")),
        )
        try:
            writer.verify()
            active_run = state.get_run(run_id)
            if not active_run or not active_run["graph_initialized"]:
                writer.reset_and_initialize(run_id, fingerprint)
                state.mark_graph_initialized(run_id)
        except Exception:
            writer.close()
            state.close()
            raise
    state_path = runtime.output_dir / "rule_extraction_state.sqlite3"
    if resume:
        state.mark_run_running(run_id)

    def process_package(package: dict[str, Any], existing: dict[str, Any] | None) -> None:
        """Process one package with its own SQLite connection.

        Neo4j's driver is thread-safe and each write has its own transaction.
        SQLite connections are not thread-safe, so each model worker owns a
        StateStore; WAL serializes the short status updates safely.
        """
        worker_state = StateStore(state_path)
        phase = "resolve"
        try:
            try:
                resolved = _resolve_with_cache(index, cache, package)
            except Exception as error:
                worker_state.set_result(_failure(package["id"], run_id, fingerprint, "resolve", "input_resolution_failed", error))
                return
            if writer is not None and existing and existing["status"] == "writing_graph":
                recovered_ids = writer.package_rule_ids(package["id"], run_id)
                if recovered_ids:
                    worker_state.set_result(PackageProcessingResult(
                        package["id"], "success", rule_ids=recovered_ids, input_fingerprint=fingerprint, run_id=run_id,
                    ))
                    return
            generator = RuleGenerator(
                models, runtime.model_key, reflector_prompt, repair_prompt,
                call_sink=lambda **event: worker_state.record_model_call(run_id, **event),
                output_sink=lambda package_id, phase, output, prompt_id, prompt_version: _store_model_output(
                    runtime.output_dir, package_id, phase, fingerprint, output, prompt_id, prompt_version,
                ),
            )
            try:
                phase = "generating"
                worker_state.set_processing(run_id, package["id"], fingerprint, phase)
                # Do not let a prior attempt's structured output appear for a
                # package that now yields no_rule, fails, or is still running.
                (runtime.output_dir / "rulesets" / f"{package['id']}.json").unlink(missing_ok=True)
                if runtime.refresh_model_output:
                    for output_phase in ("generate", "reflect", "candidate"):
                        _model_output_path(runtime.output_dir, package["id"], output_phase).unlink(missing_ok=True)
                context = generator.builder.context_content(resolved)
                active_generator_prompt, _ = routed_prompts.prompt_for(resolved)
                draft = _load_model_output(
                    runtime.output_dir, package["id"], "generate", fingerprint, active_generator_prompt,
                )
                reflection = _load_model_output(runtime.output_dir, package["id"], "reflect", fingerprint, reflector_prompt)
                if draft is None:
                    # A reflection cache without its exact-version generation draft cannot
                    # be applied safely. Rebuild both stages as one pair.
                    draft = generator.generate_draft_from_context(
                        context, package["id"], prompt=active_generator_prompt,
                    )
                    reflection = None
                if reflection is None:
                    phase = "reflecting"
                    worker_state.set_processing(run_id, package["id"], fingerprint, phase)
                    reflection, final = generator.reflect_from_context(context, package["id"], draft)
                else:
                    # Cached responses are still validated against their exact
                    # draft; malformed patches are sent back to the reflector
                    # instead of being loosened or sanitized by the parser.
                    reflection, final = generator.ensure_valid_reflection(
                        context, package["id"], draft, reflection,
                    )
                phase = "applying_reflection"
                worker_state.set_processing(run_id, package["id"], fingerprint, phase)
                _store_model_output(
                    runtime.output_dir, package["id"], "candidate", fingerprint, final,
                    reflector_prompt["id"], str(reflector_prompt["version"]),
                )
                phase = "parsing"
                worker_state.set_processing(run_id, package["id"], fingerprint, phase)
                ruleset = parse_final_expression(
                    final, package["id"],
                    prompt_id=reflector_prompt["id"],
                    prompt_version=str(reflector_prompt["version"]),
                    model=generator.model_name,
                )
                if not ruleset.rules:
                    worker_state.set_result(PackageProcessingResult(
                        package["id"], "no_rule", input_fingerprint=fingerprint, run_id=run_id,
                    ))
                    return
                phase = "writing_graph" if writer is not None else "persisting"
                worker_state.set_processing(run_id, package["id"], fingerprint, phase)
                write_ruleset(runtime.output_dir, ruleset)
                expected_ids = [rule.id for rule in ruleset.rules]
                if writer is not None:
                    writer.write_ruleset(ruleset, run_id)
                    written_ids = writer.package_rule_ids(package["id"], run_id)
                    if written_ids != sorted(expected_ids):
                        raise RuntimeError("Neo4j committed rule IDs do not match the parsed rule set")
                worker_state.set_result(PackageProcessingResult(
                    package["id"], "success", rule_ids=expected_ids,
                    input_fingerprint=fingerprint, run_id=run_id,
                ))
            except RuleParseError as error:
                worker_state.set_result(_failure(package["id"], run_id, fingerprint, "parse", "dsl_parse_failed", error))
            except Exception as error:
                worker_state.set_result(_failure(
                    package["id"], run_id, fingerprint, phase, str(getattr(error, "code", "processing_failed")), error,
                ))
        finally:
            worker_state.close()

    try:
        candidates: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
        for package in packages:
            row = state.package_state(run_id, package["id"])
            existing = dict(row) if row else None
            if existing and existing["status"] in {"success", "no_rule"}:
                continue
            if existing and existing["status"] == "failed" and not retry_failed:
                continue
            candidates.append((package, existing))
        candidate_ids = [package["id"] for package, _ in candidates]
        state.queue_packages(run_id, candidate_ids, fingerprint)
        progress = _TerminalProgress(state_path, run_id, set(candidate_ids)) if runtime.show_progress else None
        if progress:
            progress.start()
        try:
            if runtime.workers == 1:
                for package, existing in candidates:
                    process_package(package, existing)
            else:
                with ThreadPoolExecutor(max_workers=runtime.workers, thread_name_prefix="rule-extract") as executor:
                    futures = [executor.submit(process_package, package, existing) for package, existing in candidates]
                    for future in as_completed(futures):
                        future.result()
        finally:
            if progress:
                progress.stop()
        if writer:
            writer.finish_run(run_id)
        state.finish_run(run_id)
        eligible_package_ids = {package["id"] for package in all_packages}
        # A run created by an older build may still contain states for shared
        # article-only candidates. Keep those historical rows in SQLite, but do
        # not let them leak into current reports or exports.
        results = [
            result for result in state.iter_results(run_id)
            if result.context_package_id in eligible_package_ids
        ]
        counts = {status: sum(item.status == status for item in results) for status in ("success", "no_rule", "failed")}
        result_ids = {item.context_package_id for item in results}
        export_packages = [package for package in all_packages if package["id"] in result_ids]
        route_counts: Counter[str] = Counter()
        for package in export_packages:
            resolved = _resolve_with_cache(index, cache, package)
            route_counts[route_name(route_modalities(resolved))] += 1
        report = {
            "run_id": run_id, "input_fingerprint": fingerprint, "package_count": len(results), "selected_package_count": len(packages), **{f"{key}_count": value for key, value in counts.items()},
            "rule_count": sum(len(item.rule_ids) for item in results if item.status == "success"),
            "elapsed_seconds": round(state.run_elapsed_seconds(run_id), 3), "model": model,
            "prompt_versions": active_prompt_versions,
            "extraction_mode": "structure_routed",
            "route_counts": dict(sorted(route_counts.items())),
        }
        export_outputs(runtime.output_dir, export_packages, results, report)
        return report
    finally:
        if writer:
            writer.close()
        state.close()
