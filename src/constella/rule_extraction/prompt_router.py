from __future__ import annotations

from collections import Counter
from pathlib import Path
import threading
from typing import Any, Iterable

from .generator import load_prompt
from .models import ResolvedContextPackage


SPECIALIST_ORDER = ("image", "table", "formula")
ASSET_TYPE_TO_MODALITY = {
    "figure": "image",
    "image": "image",
    "table": "table",
    "formula": "formula",
}


def route_modalities(package: ResolvedContextPackage) -> tuple[str, ...]:
    """Return every evidence form present in a resolved package.

    A package may contain more than one specialist form.  In that case all
    matching prompt modules are composed; routing never chooses a winner and
    silently discards another evidence form.
    """
    unit_types = {
        unit.type.lower()
        for unit in (*package.core_units, *package.support_units)
        if unit.type
    }
    unit_types.update(asset.unit.type.lower() for asset in package.assets if asset.unit.type)
    specialists = tuple(name for name in SPECIALIST_ORDER if any(
        ASSET_TYPE_TO_MODALITY.get(unit_type) == name for unit_type in unit_types
    ))
    # Every context package is anchored by core/support text.  Specialist
    # evidence augments that anchor; it must never replace text extraction.
    return ("text", *specialists)


def route_name(modalities: Iterable[str]) -> str:
    return "+".join(modalities)


class RoutedPromptRegistry:
    """Build a generation prompt from a common contract and route addenda."""

    def __init__(self, prompt_dir: str | Path) -> None:
        prompt_dir = Path(prompt_dir)
        self.base = load_prompt(prompt_dir / "rule_generator_routed_base_v1.yaml")
        self.specialists = {
            name: load_prompt(prompt_dir / f"rule_generator_{name}_v1.yaml")
            for name in ("text", *SPECIALIST_ORDER)
        }
        self._cache: dict[tuple[str, ...], dict[str, Any]] = {}
        self.route_counts: Counter[str] = Counter()
        self._lock = threading.Lock()

    def prompt_for(self, package: ResolvedContextPackage) -> tuple[dict[str, Any], str]:
        modalities = route_modalities(package)
        name = route_name(modalities)
        with self._lock:
            self.route_counts[name] += 1
            if modalities not in self._cache:
                modules = [self.specialists[item] for item in modalities]
                versions = [f"base@{self.base['version']}"] + [
                    f"{item}@{module['version']}" for item, module in zip(modalities, modules, strict=True)
                ]
                self._cache[modalities] = {
                    "id": f"rule_generator_routed__{name.replace('+', '__')}",
                    "version": "+".join(versions),
                    "system": "\n\n".join([
                        self.base["system"],
                        "当前包证据路由：" + "、".join(modalities) + "。只执行下列命中模块，不因资源存在而强制生成规则。",
                        *(module["system"] for module in modules),
                    ]),
                }
            prompt = self._cache[modalities]
        return prompt, name

    def descriptor(self) -> dict[str, Any]:
        return {
            "mode": "routed",
            "base": str(self.base["version"]),
            "specialists": {key: str(value["version"]) for key, value in self.specialists.items()},
        }
