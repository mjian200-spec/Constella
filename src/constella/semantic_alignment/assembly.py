from __future__ import annotations

from collections import Counter, defaultdict
import json
from pathlib import Path
from typing import Any, Iterable

from .packages import AlignmentInputs, normalize_text, stable_id


class _UnionFind:
    def __init__(self, values: Iterable[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, left: str, right: str) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self.parent[max(left_root, right_root)] = min(left_root, right_root)


def assemble_concepts(
    inputs: AlignmentInputs,
    results: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str], dict[str, Any]]:
    by_id = {str(row["concept_id"]): row for row in inputs.concepts}
    union = _UnionFind(by_id)
    merge_group_count = 0
    for result in results:
        if result.get("status") != "success":
            continue
        groups = list(result["output"].get("merge_groups", []))
        groups.extend(result["output"].get("merge_pairs", []))
        for group in groups:
            merge_group_count += 1
            for concept_id in group[1:]:
                union.union(group[0], concept_id)
    groups: dict[str, list[str]] = defaultdict(list)
    for concept_id in by_id:
        groups[union.find(concept_id)].append(concept_id)

    object_frequency: Counter[str] = Counter()
    for rule in inputs.rules:
        for side in ("conditions", "antecedents", "consequents"):
            for state in rule.get(side) or []:
                object_frequency[normalize_text(str(state.get("object") or ""))] += 1

    id_map: dict[str, str] = {}
    concepts: list[dict[str, Any]] = []
    for member_ids in groups.values():
        representative = max(
            member_ids,
            key=lambda concept_id: (
                sum(object_frequency[normalize_text(name)] for name in [
                    by_id[concept_id].get("canonical_name"), *(by_id[concept_id].get("aliases") or []),
                ] if name),
                bool(by_id[concept_id].get("definition")),
                -len(str(by_id[concept_id].get("canonical_name") or "")),
                concept_id,
            ),
        )
        for member_id in member_ids:
            id_map[member_id] = representative
        source = by_id[representative]
        names: list[str] = []
        alignment_examples: list[str] = []
        for member_id in member_ids:
            row = by_id[member_id]
            for name in [row.get("canonical_name"), *(row.get("aliases") or [])]:
                if name and normalize_text(str(name)) not in {normalize_text(item) for item in names}:
                    names.append(str(name))
            for example in row.get("alignment_examples") or []:
                if example and example not in alignment_examples:
                    alignment_examples.append(str(example))
        canonical = str(source["canonical_name"])
        definitions = [by_id[value].get("definition") for value in member_ids if by_id[value].get("definition")]
        evidence_ids = list(dict.fromkeys(
            str(item) for member_id in member_ids for item in (by_id[member_id].get("evidence_ids") or [])
        ))
        package_ids = list(dict.fromkeys(
            str(item) for member_id in member_ids for item in (by_id[member_id].get("source_package_ids") or [])
        ))
        concepts.append({
            **source,
            "concept_id": representative,
            "canonical_name": canonical,
            "aliases": [name for name in names if normalize_text(name) != normalize_text(canonical)],
            "definition": source.get("definition") or (definitions[0] if definitions else None),
            "source_concept_ids": sorted(member_ids),
            "source_package_ids": package_ids,
            "evidence_ids": evidence_ids,
            "alignment_examples": alignment_examples[:10],
        })

    relations: list[dict[str, Any]] = []
    seen_relations: set[tuple[str, str, str]] = set()
    missing_relation_endpoint_count = 0
    for row in inputs.relations:
        source_child = str(row.get("child_concept_id") or "")
        source_parent = str(row.get("parent_concept_id") or "")
        if source_child not in id_map or source_parent not in id_map:
            missing_relation_endpoint_count += 1
            continue
        child = id_map[source_child]
        parent = id_map[source_parent]
        key = (child, str(row["type"]), parent)
        if child == parent or key in seen_relations:
            continue
        seen_relations.add(key)
        relations.append({**row, "child_concept_id": child, "parent_concept_id": parent})
    report = {
        "source_concept_count": len(by_id),
        "merged_concept_count": len(concepts),
        "removed_duplicate_count": len(by_id) - len(concepts),
        "llm_merge_group_count": merge_group_count,
        "source_relation_count": len(inputs.relations),
        "merged_relation_count": len(relations),
        "missing_relation_endpoint_count": missing_relation_endpoint_count,
    }
    return sorted(concepts, key=lambda row: row["concept_id"]), relations, id_map, report


def assemble_object_alignments(
    results: list[dict[str, Any]],
    concepts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    alignments: list[dict[str, Any]] = []
    new_concepts: dict[str, dict[str, Any]] = {}
    existing_concept_ids = {str(row["concept_id"]) for row in concepts}
    for result in results:
        if result.get("status") != "success":
            continue
        package_path = result.get("_package")
        object_cases = package_path or {}
        for row in result["output"].get("alignments", []):
            concept_id = row["concept_id"]
            case = object_cases.get(row["object_id"], {})
            object_name = str(case.get("name") or row["object_id"])
            alignment = {
                **row,
                "object_name": object_name,
                "frequency": int(case.get("frequency") or 0),
                "state_examples": list(case.get("states") or []),
            }
            if concept_id == "NEW":
                concept_id = stable_id("concept", {"new_object": normalize_text(object_name)})
                alignment["concept_id"] = concept_id
                if concept_id in existing_concept_ids:
                    alignment["decision"] = "ALIGNED"
                else:
                    new_concepts.setdefault(concept_id, {
                        "concept_id": concept_id,
                        "canonical_name": object_name,
                        "aliases": [],
                        "definition": None,
                        "definition_type": None,
                        "source_package_ids": [],
                        "evidence_ids": [],
                        "audit_status": "alignment_created",
                        "origin_depth": 0,
                        "source_concept_ids": [],
                        "alignment_examples": list(case.get("states") or [])[:5],
                    })
                    alignment["decision"] = "NEW"
            elif concept_id == "REPARSE":
                alignment["decision"] = "REPARSE"
            elif concept_id == "INVALID":
                alignment["decision"] = "INVALID"
            else:
                alignment["decision"] = "ALIGNED"
            alignments.append(alignment)
    final_concepts = [*concepts, *new_concepts.values()]
    counts = Counter(row["decision"] for row in alignments)
    report = {
        "alignment_count": len(alignments),
        "aligned_count": counts["ALIGNED"],
        "new_concept_count": counts["NEW"],
        "reparse_object_count": counts["REPARSE"],
        "invalid_object_count": counts["INVALID"],
        "usable_rate": round((counts["ALIGNED"] + counts["NEW"]) / len(alignments), 4) if alignments else 0.0,
        "weighted_occurrence_count": sum(int(row.get("frequency") or 0) for row in alignments),
        "weighted_usable_rate": round(
            sum(int(row.get("frequency") or 0) for row in alignments if row["decision"] in {"ALIGNED", "NEW"})
            / max(1, sum(int(row.get("frequency") or 0) for row in alignments)),
            4,
        ),
    }
    return alignments, final_concepts, report


def assemble_states(results: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    exception_counts: Counter[str] = Counter()
    for result in results:
        if result.get("status") != "success":
            continue
        concept_id = result.get("_concept_id")
        for group in result["output"].get("groups", []):
            canonical_id = stable_id("canonical_state", {
                "concept_id": concept_id, "canonical": normalize_text(group["canonical"]),
            })
            for state_id in group["members"]:
                normalized.append({
                    "state_id": state_id,
                    "concept_id": concept_id,
                    "canonical_state_id": canonical_id,
                    "canonical_state": group["canonical"],
                    "status": "normalized",
                })
        for exception in result["output"].get("exceptions", []):
            exception_counts[exception["type"]] += 1
            normalized.append({
                "state_id": exception["state_id"],
                "concept_id": concept_id,
                "canonical_state_id": None,
                "canonical_state": None,
                "status": exception["type"].lower(),
            })
    normalized_count = sum(row["status"] == "normalized" for row in normalized)
    report = {
        "state_count": len(normalized),
        "normalized_count": normalized_count,
        "normalization_success_rate": round(normalized_count / len(normalized), 4) if normalized else 0.0,
        "exception_counts": dict(exception_counts),
        "canonical_state_count": len({row["canonical_state_id"] for row in normalized if row["canonical_state_id"]}),
    }
    return normalized, report


def assemble_singleton_states(packages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize one-state concepts mechanically; no synonym decision is possible."""
    rows: list[dict[str, Any]] = []
    for package in packages:
        states = package.get("states") or []
        if len(states) != 1:
            continue
        state = states[0]
        concept_id = str(package["concept"]["id"])
        canonical = str(state.get("current_normalized") or state.get("text") or "").strip()
        rows.append({
            "state_id": state["id"],
            "concept_id": concept_id,
            "canonical_state_id": stable_id("canonical_state", {
                "concept_id": concept_id, "canonical": normalize_text(canonical),
            }),
            "canonical_state": canonical,
            "status": "normalized",
            "normalization_method": "singleton_passthrough",
        })
    return rows


def assemble_state_object_alignments(
    results: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for result in results:
        if result.get("status") != "success":
            continue
        cases = result.get("_package") or {}
        for decision in result["output"].get("alignments", []):
            case = cases.get(decision["state_id"], {})
            concept_id = decision["concept_id"]
            rows.append({
                **decision,
                "source_object_id": case.get("source_object_id"),
                "object_name": case.get("object_name"),
                "state_text": case.get("state_text"),
                "frequency": int(case.get("frequency") or 0),
                "decision": "ALIGNED" if concept_id not in {"INVALID", "UNRESOLVED"} else concept_id,
            })
    counts = Counter(row["decision"] for row in rows)
    total_weight = sum(int(row.get("frequency") or 0) for row in rows)
    aligned_weight = sum(
        int(row.get("frequency") or 0) for row in rows if row["decision"] == "ALIGNED"
    )
    report = {
        "state_alignment_count": len(rows),
        "aligned_count": counts["ALIGNED"],
        "invalid_count": counts["INVALID"],
        "unresolved_count": counts["UNRESOLVED"],
        "state_alignment_rate": round(counts["ALIGNED"] / len(rows), 4) if rows else 0.0,
        "weighted_occurrence_count": total_weight,
        "weighted_alignment_rate": round(aligned_weight / total_weight, 4) if total_weight else 0.0,
    }
    return rows, report


def assemble_state_repairs(
    results: list[dict[str, Any]],
    concepts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    new_concepts: dict[str, dict[str, Any]] = {}
    source_decisions: Counter[str] = Counter()
    source_weights: Counter[str] = Counter()
    for result in results:
        if result.get("status") != "success":
            continue
        cases = result.get("_package") or {}
        output = result["output"]
        for terminal, decision in (("UNRESOLVED", "UNRESOLVED"), ("INVALID", "INVALID")):
            for state_id in output.get(f"{terminal.lower()}_ids", []):
                case = cases.get(state_id, {})
                frequency = int(case.get("frequency") or 0)
                source_decisions[decision] += 1
                source_weights[decision] += frequency
                rows.append({
                    "source_state_id": state_id,
                    "derived_state_id": None,
                    "concept_id": decision,
                    "object_name": case.get("object_name"),
                    "state_text": case.get("state_text"),
                    "frequency": frequency,
                    "decision": decision,
                    "contexts": list(case.get("contexts") or []),
                })
        for repair in output.get("repairs", []):
            state_id = repair["state_id"]
            case = cases.get(state_id, {})
            frequency = int(case.get("frequency") or 0)
            source_decisions["REPAIRED"] += 1
            source_weights["REPAIRED"] += frequency
            for part in repair["parts"]:
                object_name = str(part["object_name"]).strip()
                state_text = str(part["state_text"]).strip()
                concept_id = part["concept_id"]
                decision = "ALIGNED"
                if concept_id == "NEW":
                    concept_id = stable_id("concept", {"state_repair": normalize_text(object_name)})
                    decision = "NEW"
                    new_concepts.setdefault(concept_id, {
                        "concept_id": concept_id,
                        "canonical_name": object_name,
                        "aliases": [],
                        "definition": None,
                        "definition_type": None,
                        "source_package_ids": [],
                        "evidence_ids": [],
                        "audit_status": "state_repair_created",
                        "origin_depth": 0,
                        "source_concept_ids": [],
                        "alignment_examples": [state_text],
                    })
                derived_id = stable_id("derived_state", {
                    "source_state_id": state_id,
                    "concept_id": concept_id,
                    "object_name": normalize_text(object_name),
                    "state_text": normalize_text(state_text),
                })
                rows.append({
                    "source_state_id": state_id,
                    "derived_state_id": derived_id,
                    "concept_id": concept_id,
                    "object_name": object_name,
                    "state_text": state_text,
                    "frequency": frequency,
                    "decision": decision,
                    "contexts": list(case.get("contexts") or []),
                })
    total_sources = sum(source_decisions.values())
    total_weight = sum(source_weights.values())
    report = {
        "source_state_count": total_sources,
        "repaired_source_count": source_decisions["REPAIRED"],
        "remaining_unresolved_count": source_decisions["UNRESOLVED"],
        "invalid_count": source_decisions["INVALID"],
        "derived_state_count": sum(row.get("derived_state_id") is not None for row in rows),
        "new_concept_count": len(new_concepts),
        "repair_rate": round(source_decisions["REPAIRED"] / total_sources, 4) if total_sources else 0.0,
        "weighted_repair_rate": round(source_weights["REPAIRED"] / total_weight, 4) if total_weight else 0.0,
    }
    return rows, [*concepts, *new_concepts.values()], report


def remap_alignment_concepts(
    rows: list[dict[str, Any]],
    id_map: dict[str, str],
) -> list[dict[str, Any]]:
    """Apply a concept-fusion ID map without losing the original alignment decision."""
    remapped: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        concept_id = str(row.get("concept_id") or "")
        if concept_id in id_map:
            row["concept_id"] = id_map[concept_id]
            if row.get("decision") == "NEW":
                row["source_decision"] = "NEW"
                row["decision"] = "ALIGNED"
        remapped.append(row)
    return remapped


def write_jsonl(path: str | Path, rows: Iterable[dict[str, Any]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    temporary.replace(target)


def write_json(path: str | Path, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
