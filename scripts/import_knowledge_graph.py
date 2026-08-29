#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from constella.knowledge_graph import Neo4jKnowledgeGraphImporter, load_graph_dataset


def main() -> int:
    parser = argparse.ArgumentParser(description="Import Constella rule and concept outputs into Neo4j without provenance nodes.")
    parser.add_argument("--rule-output-dir", default="outputs/rule_extraction_full_20260829")
    parser.add_argument("--concept-output-dir", default="outputs/article_concepts_full_20260829")
    parser.add_argument("--dataset-id", default="gmaw_full_20260829")
    parser.add_argument("--neo4j-config", default=str(ROOT / "configs" / "rule_extraction" / "neo4j.yaml"))
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--dry-run", action="store_true", help="Validate and count inputs without connecting to Neo4j.")
    args = parser.parse_args()

    dataset = load_graph_dataset(args.rule_output_dir, args.concept_output_dir, args.dataset_id)
    if args.dry_run:
        print(json.dumps({"dataset_id": dataset.dataset_id, **dataset.counts()}, ensure_ascii=False, indent=2))
        return 0

    config = yaml.safe_load(Path(args.neo4j_config).read_text(encoding="utf-8")) or {}
    neo4j = config.get("neo4j") or {}
    password_env = str(neo4j.get("password_env") or "CONSTELLA_NEO4J_PASSWORD")
    password = os.environ.get(password_env)
    if not password:
        raise ValueError(f"Neo4j password is required in environment variable {password_env}")
    importer = Neo4jKnowledgeGraphImporter(
        str(neo4j.get("uri") or "bolt://127.0.0.1:7200"),
        str(neo4j.get("username") or "neo4j"),
        password,
        str(neo4j.get("database") or "neo4j"),
        args.batch_size,
    )
    try:
        importer.verify()
        counts = importer.import_dataset(dataset)
    finally:
        importer.close()
    print(json.dumps({"dataset_id": dataset.dataset_id, **counts}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
