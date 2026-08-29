from __future__ import annotations

import json
from typing import Any

from .models import ResolvedContextPackage


class MultimodalMessageBuilder:
    def context_content(self, package: ResolvedContextPackage) -> list[dict[str, Any]]:
        """Render the immutable semantic package without recalling source images."""
        return [{"type": "text", "text": self._text_context(package)}]

    def _text_context(self, package: ResolvedContextPackage) -> str:
        parts = [f"上下文包编号: {package.id}"]
        if package.section_path:
            parts.append("标题路径:\n" + " > ".join(package.section_path))
        if package.constraints:
            rendered = [
                "\n".join([
                    f"- [{item.id}] {item.type}: {item.value}",
                    f"  来源 Unit: {item.source_id}",
                    f"  作用域: {json.dumps(item.scope, ensure_ascii=False)}",
                    self._unit_text(item.source_unit, indent="  "),
                ])
                for item in package.constraints
            ]
            parts.append("包条件（已由上游确定作用域）:\n" + "\n".join(rendered))
        for label, units in (("核心正文", package.core_units), ("支撑内容", package.support_units)):
            if units:
                parts.append(label + ":\n" + "\n\n".join(self._unit_text(unit) for unit in units))
        if package.assets:
            parts.append("资源文字化:\n" + "\n\n".join(self._resource_text(item.unit) for item in package.assets))
        if package.unresolved:
            parts.append("未解决信息（不得补全）:\n" + json.dumps(package.unresolved, ensure_ascii=False))
        return "\n\n".join(parts)

    @staticmethod
    def _unit_text(unit, *, indent: str = "") -> str:
        source = unit.source
        return f"{indent}[{unit.id}; type={unit.type}; page={source.get('page')}]\n{indent}{unit.content or ''}"

    @staticmethod
    def _resource_text(unit) -> str:
        attributes = unit.attributes
        understanding = attributes.get("resource_understanding") or {}
        values = [
            f"[{unit.id}; type={unit.type}; page={unit.source.get('page')}]",
            f"题注: {attributes.get('caption') or ''}",
        ]
        if unit.type == "table" and attributes.get("table_body"):
            values.append("表格正文:\n" + str(attributes["table_body"]))
        if unit.content:
            values.append("原始文字:\n" + str(unit.content))
        if understanding.get("title"):
            values.append("资源标题: " + str(understanding["title"]))
        if understanding.get("description"):
            values.append("资源描述: " + str(understanding["description"]))
        if understanding.get("summary"):
            values.append("公式摘要: " + str(understanding["summary"]))
        if understanding.get("symbols"):
            values.append("符号释义: " + json.dumps(understanding["symbols"], ensure_ascii=False))
        return "\n".join(values)
