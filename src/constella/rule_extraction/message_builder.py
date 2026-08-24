from __future__ import annotations

import json
from typing import Any

from .image_adapter import ImageAdapter
from .models import ResolvedContextPackage


class MultimodalMessageBuilder:
    def __init__(self, image_adapter: ImageAdapter | None = None) -> None:
        self.image_adapter = image_adapter or ImageAdapter()

    def context_content(self, package: ResolvedContextPackage) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = [{"type": "text", "text": self._text_context(package)}]
        for asset in package.assets:
            # MinerU tables also carry a rendered image. Send it alongside the HTML so the
            # model can use visual headers, merged cells, and symbols that OCR may lose.
            if asset.unit.type not in {"figure", "table"} or not asset.original_path:
                continue
            image = self.image_adapter.prepare(asset.resolved_path)
            blocks.append({"type": "text", "text": self._image_label(asset.unit.id, asset.caption, asset.unit.source)})
            blocks.append({"type": "image_url", "image_url": {"url": image.data_url}})
        return blocks

    def _text_context(self, package: ResolvedContextPackage) -> str:
        parts = [f"上下文包编号: {package.id}"]
        if package.section_path:
            parts.append("标题路径:\n" + " > ".join(package.section_path))
        if package.constraints:
            rendered = [f"- [{item.id}] {item.type}: {item.value}" for item in package.constraints]
            parts.append("明确约束:\n" + "\n".join(rendered))
        for label, units in (("核心正文", package.core_units), ("支撑内容", package.support_units)):
            if units:
                parts.append(label + ":\n" + "\n\n".join(self._unit_text(unit) for unit in units))
        non_figures = [asset for asset in package.assets if asset.unit.type != "figure"]
        if non_figures:
            parts.append("关联表格/公式/资产:\n" + "\n\n".join(self._unit_text(item.unit) for item in non_figures))
        if package.unresolved:
            parts.append("未解决信息（不得补全）:\n" + json.dumps(package.unresolved, ensure_ascii=False))
        return "\n\n".join(parts)

    @staticmethod
    def _unit_text(unit) -> str:
        source = unit.source
        return f"[{unit.id}; type={unit.type}; page={source.get('page')}]\n{unit.content or ''}"

    @staticmethod
    def _image_label(unit_id: str, caption: str | None, source: dict[str, Any]) -> str:
        return f"关联图片 [{unit_id}; page={source.get('page')}]: {caption or ''}"
