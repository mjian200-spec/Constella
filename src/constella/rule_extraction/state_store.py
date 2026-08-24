from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .models import PackageProcessingResult


TERMINAL_STATES = {"success", "no_rule", "failed"}


class StateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A run can have many model workers.  Each worker gets its own store and
        # connection, while WAL + a busy timeout serialize the very short state
        # updates without treating normal writer contention as an extraction error.
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self._create_schema()

    def close(self) -> None:
        self.connection.close()

    def _create_schema(self) -> None:
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
          run_id TEXT PRIMARY KEY, input_fingerprint TEXT NOT NULL, graph_initialized INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL, started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, completed_at TEXT, model TEXT, prompt_versions TEXT
        );
        CREATE TABLE IF NOT EXISTS package_states (
          run_id TEXT NOT NULL, context_package_id TEXT NOT NULL, input_fingerprint TEXT NOT NULL,
          status TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0, rule_ids_json TEXT NOT NULL DEFAULT '[]',
          failure_stage TEXT, failure_code TEXT, failure_reason TEXT, improvement_notes_json TEXT NOT NULL DEFAULT '[]',
          started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          completed_at TEXT, PRIMARY KEY(run_id, context_package_id)
        );
        CREATE TABLE IF NOT EXISTS model_calls (
          id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, context_package_id TEXT NOT NULL,
          phase TEXT NOT NULL, attempt INTEGER NOT NULL, model TEXT, prompt_id TEXT, prompt_version TEXT,
          status TEXT NOT NULL, latency_seconds REAL, input_text_chars INTEGER, image_count INTEGER,
          output_chars INTEGER, error_type TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS graph_commits (
          run_id TEXT NOT NULL, context_package_id TEXT NOT NULL, rule_ids_json TEXT NOT NULL,
          committed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(run_id, context_package_id)
        );
        """)
        self.connection.commit()

    def create_run(self, input_fingerprint: str, model: str, prompt_versions: dict[str, str]) -> str:
        run_id = f"run_{uuid.uuid4().hex[:16]}"
        self.connection.execute(
            "INSERT INTO runs(run_id,input_fingerprint,status,model,prompt_versions) VALUES(?,?,?, ?,?)",
            (run_id, input_fingerprint, "running", model, json.dumps(prompt_versions, ensure_ascii=False)),
        )
        self.connection.commit()
        return run_id

    def latest_run(self) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()

    def get_run(self, run_id: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()

    def mark_graph_initialized(self, run_id: str) -> None:
        self.connection.execute("UPDATE runs SET graph_initialized=1,updated_at=CURRENT_TIMESTAMP WHERE run_id=?", (run_id,))
        self.connection.commit()

    def package_state(self, run_id: str, package_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM package_states WHERE run_id=? AND context_package_id=?", (run_id, package_id)
        ).fetchone()

    def set_processing(self, run_id: str, package_id: str, input_fingerprint: str, status: str) -> None:
        self.connection.execute("""
          INSERT INTO package_states(run_id,context_package_id,input_fingerprint,status,attempt_count)
          VALUES(?,?,?,?,1)
          ON CONFLICT(run_id,context_package_id) DO UPDATE SET
            input_fingerprint=excluded.input_fingerprint,status=excluded.status,
            attempt_count=package_states.attempt_count+1,updated_at=CURRENT_TIMESTAMP
        """, (run_id, package_id, input_fingerprint, status))
        self.connection.commit()

    def set_result(self, result: PackageProcessingResult) -> None:
        self.connection.execute("""
          INSERT INTO package_states(run_id,context_package_id,input_fingerprint,status,rule_ids_json,
            failure_stage,failure_code,failure_reason,improvement_notes_json,completed_at)
          VALUES(?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
          ON CONFLICT(run_id,context_package_id) DO UPDATE SET
            input_fingerprint=excluded.input_fingerprint,status=excluded.status,rule_ids_json=excluded.rule_ids_json,
            failure_stage=excluded.failure_stage,failure_code=excluded.failure_code,
            failure_reason=excluded.failure_reason,improvement_notes_json=excluded.improvement_notes_json,
            updated_at=CURRENT_TIMESTAMP,completed_at=CURRENT_TIMESTAMP
        """, (
            result.run_id, result.context_package_id, result.input_fingerprint, result.status,
            json.dumps(result.rule_ids, ensure_ascii=False), result.failure_stage, result.failure_code,
            result.failure_reason, json.dumps(result.improvement_notes, ensure_ascii=False),
        ))
        self.connection.commit()

    def record_graph_commit(self, run_id: str, package_id: str, rule_ids: list[str]) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO graph_commits(run_id,context_package_id,rule_ids_json) VALUES(?,?,?)",
            (run_id, package_id, json.dumps(rule_ids, ensure_ascii=False)),
        )
        self.connection.commit()

    def record_model_call(
        self, run_id: str, package_id: str, phase: str, *, model: str, prompt_id: str,
        prompt_version: str, status: str, latency_seconds: float, input_text_chars: int,
        image_count: int, output_chars: int = 0, error_type: str | None = None,
    ) -> None:
        self.connection.execute("""
          INSERT INTO model_calls(run_id,context_package_id,phase,attempt,model,prompt_id,prompt_version,
            status,latency_seconds,input_text_chars,image_count,output_chars,error_type)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (run_id, package_id, phase, 1, model, prompt_id, prompt_version, status,
              latency_seconds, input_text_chars, image_count, output_chars, error_type))
        self.connection.commit()

    def graph_commit(self, run_id: str, package_id: str) -> list[str] | None:
        row = self.connection.execute(
            "SELECT rule_ids_json FROM graph_commits WHERE run_id=? AND context_package_id=?", (run_id, package_id)
        ).fetchone()
        return json.loads(row["rule_ids_json"]) if row else None

    def iter_results(self, run_id: str) -> Iterator[PackageProcessingResult]:
        for row in self.connection.execute("SELECT * FROM package_states WHERE run_id=? ORDER BY context_package_id", (run_id,)):
            yield PackageProcessingResult(
                context_package_id=row["context_package_id"], status=row["status"],
                rule_ids=json.loads(row["rule_ids_json"]), failure_stage=row["failure_stage"],
                failure_code=row["failure_code"], failure_reason=row["failure_reason"],
                improvement_notes=json.loads(row["improvement_notes_json"]), input_fingerprint=row["input_fingerprint"], run_id=row["run_id"],
            )

    def finish_run(self, run_id: str) -> None:
        self.connection.execute("UPDATE runs SET status='completed',completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE run_id=?", (run_id,))
        self.connection.commit()
