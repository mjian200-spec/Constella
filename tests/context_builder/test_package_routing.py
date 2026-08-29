from __future__ import annotations

from pathlib import Path
import tempfile

import yaml

from constella.context_builder.models import ContextPackage, DocumentGraph, PipelineRuntime, SourceRef, Unit
from constella.context_builder.package_routing import ROUTE_CODES, route_context_packages


ROOT = Path(__file__).resolve().parents[2]


class FakeChoiceClient:
    def __init__(self, codes):
        self.codes = iter(codes)
        self.calls = []

    def complete(self, model_key, messages, **kwargs):
        self.calls.append((model_key, messages, kwargs))
        return {"model": "fake-served", "choices": [{"message": {"content": next(self.codes)}}]}


def test_route_corpus_has_100_balanced_unique_cases():
    corpus = yaml.safe_load((ROOT / "data/context_builder/package_route_cases_v1.yaml").read_text(encoding="utf-8"))
    assert set(corpus["labels"]) == set(ROUTE_CODES.values())
    assert {label: len(rows) for label, rows in corpus["labels"].items()} == {
        "concept": 25, "rule": 25, "concept_and_rule": 25, "noise": 25,
    }
    texts = [text for rows in corpus["labels"].values() for text in rows]
    assert len(texts) == len(set(texts)) == 100


def test_package_router_uses_one_constrained_short_choice_per_package():
    graph = DocumentGraph(units={
        "u1": Unit("u1", "passage", "焊丝分为实心焊丝和药芯焊丝。", SourceRef()),
        "u2": Unit("u2", "passage", "电流增大时熔深增加。", SourceRef()),
    })
    packages = [ContextPackage("p1", ["u1"]), ContextPackage("p2", ["u2"])]
    client = FakeChoiceClient(["C", "R"])
    with tempfile.TemporaryDirectory() as directory:
        runtime = PipelineRuntime(
            ROOT / "configs/context_builder", use_package_router=True,
            package_workers=1, output_dir=Path(directory),
            model_config={"small": {"model": "fake"}},
        )
        route_context_packages(graph, packages, runtime, client=client)
    assert [package.attributes["package_role"]["label"] for package in packages] == ["concept", "rule"]
    assert all(call[2]["structured_outputs"] == {"choice": ["C", "R", "B", "N"]} for call in client.calls)
    assert all(call[2]["max_tokens"] == 4 for call in client.calls)


def test_package_router_cache_avoids_a_second_model_call():
    graph = DocumentGraph(units={"u1": Unit("u1", "passage", "目录", SourceRef())})
    package = ContextPackage("p1", ["u1"])
    with tempfile.TemporaryDirectory() as directory:
        runtime = PipelineRuntime(
            ROOT / "configs/context_builder", use_package_router=True,
            package_workers=1, output_dir=Path(directory),
            model_config={"small": {"model": "fake"}},
        )
        first = FakeChoiceClient(["N"])
        route_context_packages(graph, [package], runtime, client=first)
        second = FakeChoiceClient([])
        route_context_packages(graph, [package], runtime, client=second)
    assert len(first.calls) == 1
    assert second.calls == []
    assert package.attributes["package_role"]["is_useless"] is True
