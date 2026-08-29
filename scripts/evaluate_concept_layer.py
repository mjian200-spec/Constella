#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from constella.concept_layer.object_seeds import normalize_name


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 1.0


def main() -> int:
    parser = argparse.ArgumentParser(description="Strict structural evaluation for a Concept Layer run.")
    parser.add_argument("output_dir")
    args = parser.parse_args()
    output = Path(args.output_dir)
    seeds = read_jsonl(output / "object_seeds.jsonl")
    bundles = read_jsonl(output / "concept_evidence_bundles.jsonl")
    resolutions = read_jsonl(output / "concept_resolutions.jsonl")
    concepts = read_jsonl(output / "concepts.jsonl")
    bindings = read_jsonl(output / "rule_concept_bindings.jsonl")
    relations = read_jsonl(output / "concept_relations.jsonl")
    bundle_by_seed = {row["seed_id"]: row for row in bundles}
    seed_by_id = {row["seed_id"]: row for row in seeds}
    resolution_by_seed = {row["seed_id"]: row for row in resolutions}
    concept_ids = {row["concept_id"] for row in concepts}
    accepted = [row for row in resolutions if row["decision"] == "accepted"]
    supported_definitions = [row for row in accepted if row.get("definition_status") == "supported"]
    evidence_closed = 0
    definition_closed = 0
    for row in accepted:
        bundle = bundle_by_seed.get(row["seed_id"], {})
        evidence = bundle.get("evidence", [])
        allowed = {item["evidence_id"] for item in evidence}
        text = normalize_name("\n".join(item["text"] for item in evidence))
        seed_name = seed_by_id.get(row["seed_id"], {}).get("raw_name", "")
        names_present = bool(row.get("canonical_name")) and (
            normalize_name(row["canonical_name"]) in text
            or normalize_name(row["canonical_name"]) == normalize_name(seed_name)
        )
        ids_valid = set(row.get("evidence_ids", [])) <= allowed
        aliases_valid = all(normalize_name(alias) in text for alias in row.get("aliases", []))
        if names_present and ids_valid and aliases_valid:
            evidence_closed += 1
        definition_ids = row.get("definition_evidence_ids", [])
        if row.get("definition") and definition_ids and set(definition_ids) <= allowed:
            definition_closed += 1
    expected_binding_count = sum(
        len(seed["rule_refs"])
        for seed in seeds
        if resolution_by_seed.get(seed["seed_id"], {}).get("decision") == "accepted"
    )
    relation_valid = sum(
        row.get("type") == "IS_A"
        and row.get("directness") == "direct"
        and row.get("child_concept_id") in concept_ids
        and row.get("parent_concept_id") in concept_ids
        for row in relations
    )
    failure_prefixes = ("model_call_failed", "resolver_validation_failed", "qualification_validation_failed", "enrichment_validation_failed")
    failures = [row for row in resolutions if any(str(row.get(key) or "").startswith(failure_prefixes) for key in ("reason", "definition_reason"))]
    metrics = {
        "seed_count": len(seeds),
        "evidence_recall_coverage": ratio(sum(row["retrieval_status"] == "ready" for row in bundles), len(seeds)),
        "resolution_completeness": ratio(len(resolution_by_seed), len(seeds)),
        "decision_rate": ratio(sum(row["decision"] != "ambiguous" for row in resolutions), len(seeds)),
        "model_or_validation_failure_rate": ratio(len(failures), len(seeds)),
        "accepted_count": len(accepted),
        "accepted_evidence_closure": ratio(evidence_closed, len(accepted)),
        "definition_status_completeness": ratio(sum(row.get("definition_status") in {"supported", "insufficient_evidence", "ambiguous"} for row in accepted), len(accepted)),
        "definition_supported_rate": ratio(len(supported_definitions), len(accepted)),
        "supported_definition_evidence_closure": ratio(definition_closed, len(supported_definitions)),
        "binding_completeness": ratio(len(bindings), expected_binding_count),
        "binding_count": len(bindings),
        "direct_is_a_count": len(relations),
        "is_a_structural_validity": ratio(relation_valid, len(relations)),
    }
    gates = {
        "evidence_recall_coverage>=0.95": metrics["evidence_recall_coverage"] >= .95,
        "resolution_completeness=1": metrics["resolution_completeness"] == 1,
        "model_or_validation_failure_rate<=0.02": metrics["model_or_validation_failure_rate"] <= .02,
        "accepted_evidence_closure=1": metrics["accepted_evidence_closure"] == 1,
        "definition_status_completeness=1": metrics["definition_status_completeness"] == 1,
        "supported_definition_evidence_closure=1": metrics["supported_definition_evidence_closure"] == 1,
        "binding_completeness=1": metrics["binding_completeness"] == 1,
        "is_a_structural_validity=1": metrics["is_a_structural_validity"] == 1,
    }
    report = {
        "verdict": "PASS" if all(gates.values()) else "FAIL",
        "metrics": metrics,
        "automatic_gates": gates,
        "manual_semantic_gates": {
            "concept_eligibility_precision": ">=0.90 (review every accepted/rejected item)",
            "definition_support_precision": ">=0.90 (review every supported definition; insufficient evidence is not an error)",
            "direct_is_a_precision": "=1.00 (review every emitted relation)",
            "note": "Concept recall is not reported without a human-annotated gold set.",
        },
        "failure_seed_ids": [row["seed_id"] for row in failures],
    }
    (output / "strict_evaluation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    review = []
    for seed in seeds:
        resolution = resolution_by_seed.get(seed["seed_id"], {})
        review.append({
            "seed_id": seed["seed_id"], "seed_name": seed["raw_name"],
            "rule_ids": sorted({ref["rule_id"] for ref in seed["rule_refs"]}),
            "decision": resolution.get("decision"), "canonical_name": resolution.get("canonical_name"),
            "definition": resolution.get("definition"), "definition_status": resolution.get("definition_status"),
            "qualification_reason": resolution.get("qualification_reason"),
            "definition_reason": resolution.get("definition_reason"), "reason": resolution.get("reason"),
            "parent_decisions": resolution.get("parent_decisions", []),
            "evidence": bundle_by_seed.get(seed["seed_id"], {}).get("evidence", []),
            "manual_eligibility_correct": None, "manual_definition_supported": None,
            "manual_parent_correct": None, "manual_note": "",
        })
    (output / "semantic_review_bundle.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False)+"\n" for row in review), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
