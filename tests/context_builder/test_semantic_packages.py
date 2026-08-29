from __future__ import annotations

from pathlib import Path

from constella.context_builder.models import DocumentGraph, PipelineRuntime, SourceRef, Unit
from constella.context_builder.packages import build_context_packages


ROOT = Path(__file__).resolve().parents[2]


def unit(unit_id, kind, content, *, index, attributes=None):
    return Unit(unit_id, kind, content, SourceRef(page=0, bbox=[0, index, 10, index + 1]), attributes=attributes or {})


def runtime():
    return PipelineRuntime(ROOT / "configs/context_builder")


def test_useful_asset_owns_its_description_in_one_package():
    graph = DocumentGraph(units={
        "u1": unit("u1", "passage", "焊接系统组成如图1-1所示。", index=1),
        "u2": unit("u2", "figure", "图1-1 焊接系统", index=2, attributes={
            "caption": "图1-1 焊接系统",
            "resource_understanding": {"status": "ok", "useful": True, "title": "焊接系统组成", "description": "由电源、送丝机构和焊枪组成。"},
        }),
        "u3": unit("u3", "passage", "这是后续普通说明。", index=3),
    })
    graph.add_relation("u1", "u2", "MENTIONS")
    packages = build_context_packages(graph, runtime())
    asset = next(package for package in packages if package.asset_part_ids == ["u2"])
    assert asset.core_unit_ids == ["u1"]
    assert asset.attributes["resource_title"] == "焊接系统组成"
    assert sum("u1" in package.core_unit_ids for package in packages) == 1
    assert any(package.core_unit_ids == ["u3"] for package in packages)


def test_vlm_rejected_asset_does_not_create_an_asset_package():
    graph = DocumentGraph(units={
        "u1": unit("u1", "figure", "装饰图", index=1, attributes={
            "caption": "装饰图", "resource_understanding": {"status": "ok", "useful": False, "description": ""},
        }),
    })
    packages = build_context_packages(graph, runtime())
    assert packages == []


def test_formula_package_contains_introduction_following_symbol_text_and_formula():
    graph = DocumentGraph(units={
        "u1": unit("u1", "passage", "热输入计算式如下：", index=1),
        "u2": unit("u2", "formula", "Q=UI/v", index=2),
        "u3": unit("u3", "passage", "式中U为电压，I为电流，v为速度。", index=3),
    })
    graph.add_relation("u1", "u2", "INTRODUCES")
    graph.add_relation("u2", "u3", "EXPLAINED_BY")
    packages = build_context_packages(graph, runtime())
    formula = next(package for package in packages if package.asset_part_ids == ["u2"])
    assert formula.attributes["package_type"] == "formula_context"
    assert formula.core_unit_ids == ["u1"]
    assert "u3" in formula.support_unit_ids
    assert sum("u1" in package.core_unit_ids for package in packages) == 1


def test_structural_heading_and_contiguous_lists_are_one_package():
    graph = DocumentGraph(units={
        "u1": unit("u1", "title", "焊丝的分类", index=1),
        "u2": unit("u2", "passage", "1）实心焊丝", index=2),
        "u3": unit("u3", "passage", "2）药芯焊丝", index=3),
        "u4": unit("u4", "passage", "后续说明。", index=4),
    })
    packages = build_context_packages(graph, runtime())
    heading = next(package for package in packages if package.core_unit_ids[0] == "u1")
    assert heading.core_unit_ids == ["u1", "u2", "u3"]
    assert sum(any(item in package.core_unit_ids for item in ("u2", "u3")) for package in packages) == 1
