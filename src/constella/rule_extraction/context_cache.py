from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

from .models import ResolvedAsset, ResolvedConstraint, ResolvedContextPackage, ResolvedUnit


class ContextCache:
    def __init__(self, cache_dir: str | Path) -> None:
        self.directory = Path(cache_dir)
        self.directory.mkdir(parents=True, exist_ok=True)

    def path_for(self, package_id: str) -> Path:
        return self.directory / f"{package_id}.json"

    def write(self, package: ResolvedContextPackage) -> Path:
        path = self.path_for(package.id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(package), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
        return path

    def load(self, package_id: str, source_fingerprint: str) -> ResolvedContextPackage | None:
        path = self.path_for(package_id)
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("source_fingerprint") != source_fingerprint:
                return None
            return ResolvedContextPackage(
                id=raw["id"], core_units=[ResolvedUnit(**item) for item in raw["core_units"]],
                support_units=[ResolvedUnit(**item) for item in raw["support_units"]],
                constraints=[ResolvedConstraint(
                    **{**item, "source_unit": ResolvedUnit(**item["source_unit"])}
                ) for item in raw["constraints"]],
                assets=[ResolvedAsset(unit=ResolvedUnit(**item["unit"]), original_path=item.get("original_path"),
                                      resolved_path=item.get("resolved_path"), caption=item.get("caption")) for item in raw["assets"]],
                unresolved=raw.get("unresolved", []), section_path=raw.get("section_path", []),
                source_package=raw["source_package"], source_fingerprint=raw["source_fingerprint"],
                resolver_version=raw["resolver_version"],
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None
