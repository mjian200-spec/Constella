"""Local, live review server for rule extraction runs."""

from __future__ import annotations

import json
import mimetypes
import sqlite3
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Type
from urllib.parse import unquote, urlparse

from .prompt_router import route_modalities_for_types, route_name
from .resolver import (
    DocumentGraphIndex,
    InputResolutionError,
    is_rule_extraction_package,
    iter_packages,
    resolve_package,
)


class RuleReviewData:
    def __init__(self, context_dir: Path, extraction_dir: Path, package_ids: set[str] | None = None) -> None:
        self.context_dir, self.extraction_dir = context_dir, extraction_dir
        self.index = DocumentGraphIndex.load(context_dir / "document_graph.json")
        self.packages = {
            item["id"]: item for item in iter_packages(context_dir / "context_packages.jsonl")
            if is_rule_extraction_package(item) and (package_ids is None or item["id"] in package_ids)
        }
        self.feedback_path = extraction_dir / "manual_feedback.jsonl"
        self.lock = threading.Lock()

    def _snapshot(self, package_id: str | None = None) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]]]:
        path = self.extraction_dir / "rule_extraction_state.sqlite3"
        if not path.is_file():
            return None, {}
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            run_row = connection.execute(
                "SELECT run_id, status, started_at, completed_at, model, prompt_versions "
                "FROM runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            if not run_row:
                return None, {}
            run = dict(run_row)
            try:
                run["prompt_versions"] = json.loads(run.get("prompt_versions") or "{}")
            except (TypeError, ValueError):
                run["prompt_versions"] = {}
            generator = run["prompt_versions"].get("generator")
            run["extraction_mode"] = (
                "structure_routed" if isinstance(generator, dict) and generator.get("mode") == "routed"
                else "legacy_uniform"
            )
            if package_id is None:
                rows = connection.execute("SELECT * FROM package_states WHERE run_id=?", (run["run_id"],)).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM package_states WHERE run_id=? AND context_package_id=?",
                    (run["run_id"], package_id),
                ).fetchall()
            return run, {row["context_package_id"]: dict(row) for row in rows}
        finally:
            connection.close()

    def feedback(self) -> dict[str, dict[str, Any]]:
        if not self.feedback_path.is_file():
            return {}
        result: dict[str, dict[str, Any]] = {}
        for line in self.feedback_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    item = json.loads(line)
                    result[item["context_package_id"]] = item
                except (ValueError, KeyError, TypeError):
                    continue
        return result

    def summary(self) -> dict[str, Any]:
        run, states = self._snapshot()
        run_id = run["run_id"] if run else None
        feedback = self.feedback()
        records = []
        route_counts: dict[str, int] = {}
        visible_packages = (
            [package for package_id, package in self.packages.items() if package_id in states]
            if run_id and states else list(self.packages.values())
        )
        for package in visible_packages:
            state = states.get(package["id"], {})
            rule_ids = json.loads(state.get("rule_ids_json", "[]"))
            package_feedback = feedback.get(package["id"])
            core = self.index.units.get((package.get("core_unit_ids") or [None])[0]) or {}
            route = self._expected_route(package)
            route_counts[route["name"]] = route_counts.get(route["name"], 0) + 1
            records.append({
                "id": package["id"], "status": state.get("status", "pending"), "rule_count": len(rule_ids),
                "section_path": (package.get("attributes") or {}).get("section_path", []),
                "snippet": str(core.get("content") or ""),
                "review_status": package_feedback.get("verdict") if package_feedback else "unreviewed",
                "route": route,
            })
        records.sort(key=lambda item: item["id"])
        counts: dict[str, int] = {}
        for record in records:
            counts[record["status"]] = counts.get(record["status"], 0) + 1
        terminal = sum(counts.get(status, 0) for status in ("success", "no_rule", "failed"))
        active = sum(counts.get(status, 0) for status in (
            "generating", "reflecting", "applying_reflection", "parsing", "persisting", "writing_graph",
        ))
        elapsed_seconds = self._elapsed_seconds(run)
        throughput = terminal / elapsed_seconds * 60 if elapsed_seconds and terminal else 0.0
        remaining_seconds = (len(records) - terminal) / (terminal / elapsed_seconds) if elapsed_seconds and terminal else None
        total_rules = sum(record["rule_count"] for record in records)
        feedback_counts = {
            "appropriate": sum(record["review_status"] == "appropriate" for record in records),
            "inappropriate": sum(record["review_status"] == "inappropriate" for record in records),
            "unreviewed": sum(record["review_status"] == "unreviewed" for record in records),
        }
        return {
            "run_id": run_id, "run": run, "counts": counts, "package_count": len(records), "packages": records,
            "result_stats": {
                "total_rules": total_rules,
                "average_rules": total_rules / len(records) if records else 0.0,
                "over_20": sum(record["rule_count"] > 20 for record in records),
                "over_40": sum(record["rule_count"] > 40 for record in records),
                "max_rules": max((record["rule_count"] for record in records), default=0),
            },
            "feedback_counts": feedback_counts,
            "route_counts": dict(sorted(route_counts.items())),
            "progress": {
                "completed": terminal, "active": active, "queued": max(0, len(records) - terminal - active),
                "elapsed_seconds": elapsed_seconds, "throughput_per_minute": throughput,
                "estimated_remaining_seconds": 0.0 if run and run.get("status") == "completed" else remaining_seconds,
            },
        }

    def _elapsed_seconds(self, run: dict[str, Any] | None) -> float | None:
        if not run or not run.get("started_at"):
            return None
        try:
            started = datetime.strptime(run["started_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            finished_at = run.get("completed_at")
            finished = (
                datetime.strptime(finished_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                if finished_at else datetime.now(timezone.utc)
            )
            return max(0.0, (finished - started).total_seconds())
        except (TypeError, ValueError):
            return None

    def package_detail(self, package_id: str) -> dict[str, Any]:
        package = self.packages.get(package_id)
        if not package:
            raise KeyError(package_id)
        resolved = resolve_package(self.index, package)
        run, states = self._snapshot(package_id)
        run_id = run["run_id"] if run else None
        state = states.get(package_id, {})
        model_outputs = {
            phase: self._model_output_record(package_id, phase) for phase in ("generate", "reflect", "candidate")
        }
        expected_route = self._expected_route(package)
        actual_route = self._actual_route(model_outputs.get("generate"))
        if actual_route is None:
            route_status = "pending"
        elif actual_route["name"] == "legacy-uniform":
            route_status = "legacy"
        else:
            route_status = "matched" if actual_route.get("modalities") == expected_route["modalities"] else "mismatch"
        # Ruleset files are an on-disk cache shared across resumptions. Never
        # expose one from an earlier attempt when the latest state is no_rule,
        # failed, or still processing.
        ruleset = self._ruleset(package_id) if state.get("status") == "success" else None
        return {
            "package": package, "resolved": asdict(resolved), "run_id": run_id,
            "state": {**state, "rule_ids": json.loads(state.get("rule_ids_json", "[]"))},
            "route": {
                "expected": expected_route,
                "actual": actual_route,
                "status": route_status,
            },
            "model_outputs": model_outputs,
            "ruleset": ruleset, "feedback": self.feedback().get(package_id),
        }

    def _expected_route(self, package: dict[str, Any]) -> dict[str, Any]:
        unit_ids = [
            *(package.get("core_unit_ids") or []),
            *(package.get("support_unit_ids") or []),
            *(package.get("asset_part_ids") or []),
        ]
        unit_types = [
            str((self.index.units.get(unit_id) or {}).get("type") or "")
            for unit_id in unit_ids
        ]
        modalities = route_modalities_for_types(unit_types)
        return {"name": route_name(modalities), "modalities": list(modalities)}

    @staticmethod
    def _actual_route(record: dict[str, Any] | None) -> dict[str, Any] | None:
        if not record:
            return None
        prompt_id = str(record.get("prompt_id") or "")
        prefix = "rule_generator_routed__"
        if prompt_id.startswith(prefix):
            modalities = [item for item in prompt_id[len(prefix):].split("__") if item]
            return {
                "name": route_name(modalities), "modalities": modalities,
                "prompt_id": prompt_id, "prompt_version": str(record.get("prompt_version") or ""),
            }
        return {
            "name": "legacy-uniform", "modalities": [],
            "prompt_id": prompt_id or None, "prompt_version": str(record.get("prompt_version") or ""),
        }

    def asset_path(self, package_id: str, unit_id: str) -> Path:
        package = self.packages.get(package_id)
        if not package:
            raise KeyError(package_id)
        for asset in resolve_package(self.index, package).assets:
            if asset.unit.id == unit_id and asset.resolved_path:
                path = Path(asset.resolved_path)
                if path.is_file():
                    return path
        raise KeyError(unit_id)

    def save_feedback(self, payload: dict[str, Any]) -> dict[str, Any]:
        package_id = str(payload.get("context_package_id", ""))
        verdict = payload.get("verdict")
        standard_result = str(payload.get("standard_result", "")).strip()
        note = str(payload.get("note", "")).strip()
        if package_id not in self.packages:
            raise ValueError("Unknown ContextPackage")
        if verdict not in {"appropriate", "inappropriate"}:
            raise ValueError("verdict must be appropriate or inappropriate")
        if verdict == "inappropriate" and not standard_result:
            raise ValueError("A standard result is required for inappropriate feedback")
        record = {
            "context_package_id": package_id, "verdict": verdict, "standard_result": standard_result,
            "note": note, "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        self.extraction_dir.mkdir(parents=True, exist_ok=True)
        with self.lock:
            with self.feedback_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
        return record

    def _model_output_record(self, package_id: str, phase: str) -> dict[str, Any] | None:
        path = self.extraction_dir / "cache" / "model_outputs" / package_id / f"{phase}.json"
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                return None
            return {
                "output": value.get("output"),
                "prompt_id": value.get("prompt_id"),
                "prompt_version": value.get("prompt_version"),
            }
        except (OSError, ValueError):
            return None

    def _ruleset(self, package_id: str) -> dict[str, Any] | None:
        path = self.extraction_dir / "rulesets" / f"{package_id}.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


def make_rule_review_handler(web_dir: Path, data: RuleReviewData) -> Type[SimpleHTTPRequestHandler]:
    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = unquote(urlparse(self.path).path)
            try:
                if path == "/" or path == "/index.html":
                    return self._file(web_dir / "index.html")
                if path in {"/app.js", "/styles.css"}:
                    return self._file(web_dir / path.lstrip("/"))
                if path == "/api/summary":
                    return self._json(data.summary())
                if path.startswith("/api/package/"):
                    return self._json(data.package_detail(path.rsplit("/", 1)[-1]))
                if path.startswith("/asset/"):
                    _, _, package_id, unit_id = path.split("/", 3)
                    return self._file(data.asset_path(package_id, unit_id))
                self.send_error(HTTPStatus.NOT_FOUND, "Unknown review resource")
            except (KeyError, InputResolutionError):
                self.send_error(HTTPStatus.NOT_FOUND, "Package or asset not found")
            except Exception as error:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

        def do_POST(self) -> None:  # noqa: N802
            if urlparse(self.path).path != "/api/feedback":
                self.send_error(HTTPStatus.NOT_FOUND, "Unknown review resource")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                self._json(data.save_feedback(payload), status=HTTPStatus.CREATED)
            except (ValueError, json.JSONDecodeError) as error:
                self.send_error(HTTPStatus.BAD_REQUEST, str(error))

        def _json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _file(self, path: Path) -> None:
            if not path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND, "Review resource not found")
                return
            body = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            print(f"[rule-review] {self.address_string()} - {format % args}")
    return Handler


def serve_rule_review(web_dir: str | Path, context_dir: str | Path, extraction_dir: str | Path, host: str = "127.0.0.1", port: int = 8766, package_ids: set[str] | None = None) -> None:
    data = RuleReviewData(Path(context_dir), Path(extraction_dir), package_ids)
    server = ThreadingHTTPServer((host, port), make_rule_review_handler(Path(web_dir), data))
    print(f"Rule Extraction review: http://{host}:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
