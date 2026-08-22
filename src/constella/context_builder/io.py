from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import DocumentGraph, SourceRef, Unit
from .models.context import ContextPackage


TYPE_MAP = {
    "text": "passage", "list": "passage", "aside_text": "passage",
    "header": "passage", "footer": "passage", "page_number": "passage",
    "image": "figure", "table": "table", "equation": "formula",
}


def load_mineru_document(input_path: str | Path) -> DocumentGraph:
    path = Path(input_path)
    blocks = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(blocks, list):
        raise ValueError("Expected a MinerU content-list JSON array")
    graph = DocumentGraph(metadata={"input_path": str(path), "adapter": "mineru_content_list_v1"})
    prefix = re.sub(r"\W+", "_", path.stem).strip("_").lower()
    for index, block in enumerate(blocks):
        if not isinstance(block, dict) or "type" not in block:
            continue
        raw_type = str(block["type"])
        unit_type = TYPE_MAP.get(raw_type, "passage")
        content = _content(block, raw_type)
        source = SourceRef(
            page=block.get("page_idx"), bbox=block.get("bbox"),
            original_block_id=f"{prefix}:{index}", asset_path=block.get("img_path"),
        )
        attributes: dict[str, Any] = {
            "mineru_type": raw_type, "source_index": index,
            "raw_fields": {key: value for key, value in block.items() if key not in {"text", "table_body"}},
        }
        if raw_type in {"header", "footer", "page_number"}:
            attributes["layout_role"] = raw_type
        if block.get("text_level") is not None:
            attributes["text_level"] = block["text_level"]
        if unit_type in {"figure", "table"}:
            captions = block.get("image_caption") or block.get("table_caption") or []
            attributes["caption"] = " ".join(captions)
            attributes["asset_label"] = _asset_label(attributes["caption"], unit_type)
            attributes["table_body"] = block.get("table_body")
        graph.units[f"unit_{index:06d}"] = Unit(f"unit_{index:06d}", unit_type, content, source, attributes=attributes)
    return graph


def _content(block: dict[str, Any], raw_type: str) -> str | dict[str, Any] | list[Any] | None:
    if raw_type == "table":
        return block.get("table_body") or ""
    if raw_type == "image":
        return " ".join(block.get("image_caption", [])) or block.get("img_path")
    if raw_type == "list":
        return "\n".join(block.get("list_items", []))
    return block.get("text") or block.get("equation") or ""


def _asset_label(caption: str, unit_type: str) -> str | None:
    kind = "图" if unit_type == "figure" else "表"
    hit = re.search(rf"{kind}\s*(\d+)\s*[-—－]\s*(\d+)", caption)
    return f"{kind}{hit.group(1)}-{hit.group(2)}" if hit else None


def save_context_outputs(graph: DocumentGraph, packages: list[ContextPackage], output_dir: str | Path) -> None:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "document_graph.json").write_text(
        json.dumps(graph.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _write_jsonl(directory / "context_packages.jsonl", [package.to_dict() for package in packages])
    structured_candidates = [
        {"unit_id": unit.id, "content": unit.content, "source": asdict(unit.source), "roles": unit.role}
        for unit in graph.units.values() if "structured_candidate" in unit.role
    ]
    # Filename remains part of the output contract; records are neutral structured candidates.
    _write_jsonl(directory / "ontology_candidates.jsonl", structured_candidates)
    _write_jsonl(directory / "ambiguities.jsonl", [asdict(item) for item in graph.ambiguities.values()])
    report = {
        "unit_count": len(graph.units), "relation_count": len(graph.relations),
        "constraint_count": len(graph.constraints), "ambiguity_count": len(graph.ambiguities),
        "context_package_count": len(packages), "model_calls": graph.metadata.get("model_calls", []),
        "run_events": graph.metadata.get("run_events", []),
        "elapsed_seconds": graph.metadata.get("elapsed_seconds"), "exceptions": [],
    }
    (directory / "run_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), encoding="utf-8")
