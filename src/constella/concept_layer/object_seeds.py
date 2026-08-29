from __future__ import annotations
from collections import defaultdict
import re
from .input_index import ConceptInputIndex, stable_hash
from .models import ObjectSeed, RuleObjectRef

def normalize_name(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(value or "")).lower()

def build_object_seeds(index: ConceptInputIndex) -> list[ObjectSeed]:
    grouped: dict[str, list[RuleObjectRef]] = defaultdict(list); names: dict[str, str] = {}
    for rule in index.iter_rules():
        for side in ("conditions", "antecedents", "consequents"):
            for position, expr in enumerate(rule.get(side) or []):
                raw = str(expr.get("object") or "").strip(); key = normalize_name(raw)
                if not key: continue
                names.setdefault(key, raw)
                grouped[key].append(RuleObjectRef(str(rule.get("id") or ""), str(rule.get("context_package_id") or ""), str(expr.get("id") or ""), side.removesuffix("s"), position, raw))
    return [ObjectSeed(stable_hash("seed", key), names[key], key, refs) for key, refs in sorted(grouped.items())]
