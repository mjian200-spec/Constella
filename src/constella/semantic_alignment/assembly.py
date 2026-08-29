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
        for member_id in member_ids:
            row = by_id[member_id]
            for name in [row.get("canonical_name"), *(row.get("aliases") or [])]:
                if name and normalize_text(str(name)) not in {normalize_text(item) for item in names}:
                    names.append(str(name))
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
        })

    relations: list[dict[str, Any]] = []
    seen_relations: set[tuple[str, str, str]] = set()
    for row in inputs.relations:
        child = id_map[str(row["child_concept_id"])]
        parent = id_map[str(row["parent_concept_id"])]
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
    }
    return sorted(concepts, key=lambda row: row["concept_id"]), relations, id_map, report


def assemble_object_alignments(
    results: list[dict[str, Any]],
    concepts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    alignments: list[dict[str, Any]] = []
    new_concepts: dict[str, dict[str, Any]] = {}
    for result in results:
        if result.get("status") != "success":
            continue
        package_path = result.get("_package")
        object_names = package_path or {}
        for row in result["output"].get("alignments", []):
            concept_id = row["concept_id"]
            object_name = str(object_names.get(row["object_id"], row["object_id"]))
            alignment = {**row, "object_name": object_name}
            if concept_id == "NEW":
                concept_id = stable_id("concept", {"new_object": normalize_text(object_name)})
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
                })
                alignment["concept_id"] = concept_id
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
