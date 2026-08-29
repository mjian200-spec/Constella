#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from constella.knowledge_graph.viewer_server import serve_knowledge_graph


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the Constella Neo4j Knowledge Graph Viewer.")
    parser.add_argument("--dataset-id", default="gmaw_full_20260829")
    parser.add_argument("--neo4j-config", default=str(ROOT / "configs" / "rule_extraction" / "neo4j.yaml"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()
    config = yaml.safe_load(Path(args.neo4j_config).read_text(encoding="utf-8")) or {}
    neo4j = config.get("neo4j") or {}
    password_env = str(neo4j.get("password_env") or "CONSTELLA_NEO4J_PASSWORD")
    password = os.environ.get(password_env)
    if not password:
        raise ValueError(f"Neo4j password is required in environment variable {password_env}")
    serve_knowledge_graph(
        ROOT / "web" / "knowledge_graph",
        str(neo4j.get("uri") or "bolt://127.0.0.1:7200"),
        str(neo4j.get("username") or "neo4j"),
        password,
        str(neo4j.get("database") or "neo4j"),
        args.dataset_id,
        args.host,
        args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
