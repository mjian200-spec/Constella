from __future__ import annotations

import json
from pathlib import Path
import tempfile

from PIL import Image

from constella.context_builder.models import DocumentGraph, PipelineRuntime, SourceRef, Unit
from constella.context_builder.resource_understanding import understand_document_resources


ROOT = Path(__file__).resolve().parents[2]


class FakeResourceClient:
    def __init__(self):
        self.calls = []

    def complete(self, model_key, messages, **kwargs):
        self.calls.append((model_key, messages, kwargs))
        if kwargs["prompt_id"] == "resource_textualizer":
            value = {
                "useful": True, "title": "焊接系统组成",
                "description": "焊接系统由电源、送丝机构和焊枪组成。",
                "content_kinds": ["composition"],
            }
        else:
            value = {
                "summary": "热输入与电流、电压成正比，与速度成反比。",
                "symbols": [{
                    "symbol": "I", "meaning": "焊接电流", "unit": "A",
                    "evidence_unit_ids": ["u3"],
                }],
            }
        return {"model": "fake-served", "choices": [{"message": {"content": json.dumps(value, ensure_ascii=False)}}]}


def test_resource_understanding_sends_real_image_and_resolves_formula_symbols():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        Image.new("RGB", (4, 4), "white").save(root / "figure.png")
        graph = DocumentGraph(
            units={
                "u1": Unit("u1", "passage", "系统组成如图1-1所示。", SourceRef()),
                "u2": Unit("u2", "figure", "图1-1 焊接系统", SourceRef(asset_path="figure.png"), attributes={"caption": "图1-1 焊接系统"}),
                "u3": Unit("u3", "passage", "式中I为焊接电流，单位A。", SourceRef()),
                "u4": Unit("u4", "formula", "Q=UI/v", SourceRef()),
            },
            metadata={"input_path": str(root / "document.json"), "reading_order": ["u1", "u2", "u4", "u3"]},
        )
        graph.add_relation("u1", "u2", "MENTIONS")
        graph.add_relation("u4", "u3", "EXPLAINED_BY")
        client = FakeResourceClient()
        runtime = PipelineRuntime(
            ROOT / "configs/context_builder", use_resource_llm=True,
            resource_workers=1, output_dir=root / "output",
            model_config={"vision": {"model": "fake"}},
        )
        understand_document_resources(graph, runtime, client=client)

    asset_call = next(call for call in client.calls if call[2]["prompt_id"] == "resource_textualizer")
    content = asset_call[1][1]["content"]
    assert any(block["type"] == "image_url" and block["image_url"]["url"].startswith("data:image/png;base64,") for block in content)
    assert graph.units["u2"].attributes["resource_understanding"]["useful"] is True
    assert graph.units["u4"].attributes["resource_understanding"]["symbols"][0]["meaning"] == "焊接电流"


def test_resource_failure_is_recorded_without_aborting_other_units():
    class FailingClient:
        def complete(self, *args, **kwargs):
            raise TimeoutError("model unavailable")

    with tempfile.TemporaryDirectory() as directory:
        graph = DocumentGraph(
            units={"u1": Unit("u1", "table", "<table></table>", SourceRef(), attributes={"table_body": "<table></table>"})},
            metadata={"reading_order": ["u1"]},
        )
        runtime = PipelineRuntime(
            ROOT / "configs/context_builder", use_resource_llm=True,
            resource_workers=1, output_dir=Path(directory),
            model_config={"vision": {"model": "fake"}},
        )
        understand_document_resources(graph, runtime, client=FailingClient())
    result = graph.units["u1"].attributes["resource_understanding"]
    assert result["status"] == "failed"
    assert result["error_type"] == "TimeoutError"


def test_non_list_resource_content_kinds_are_normalized_to_list():
    class SingleKindClient:
        calls = 0

        def complete(self, *args, **kwargs):
            self.calls += 1
            value = {
                "useful": True,
                "title": "焊接结构",
                "description": "展示焊接结构。",
                "content_kinds": "composition" if self.calls == 1 else {"composition": True, "other": False},
            }
            return {"model": "fake-served", "choices": [{"message": {"content": json.dumps(value)}}]}

    with tempfile.TemporaryDirectory() as directory:
        graph = DocumentGraph(
            units={
                "u1": Unit("u1", "table", "<table></table>", SourceRef()),
                "u2": Unit("u2", "table", "<table></table>", SourceRef()),
            },
            metadata={"reading_order": ["u1", "u2"]},
        )
        runtime = PipelineRuntime(
            ROOT / "configs/context_builder", use_resource_llm=True,
            resource_workers=1, output_dir=Path(directory),
            model_config={"vision": {"model": "fake"}},
        )
        understand_document_resources(graph, runtime, client=SingleKindClient())

    result = graph.units["u1"].attributes["resource_understanding"]
    assert result["status"] == "ok"
    assert result["content_kinds"] == ["composition"]
    assert graph.units["u2"].attributes["resource_understanding"]["content_kinds"] == ["composition"]
