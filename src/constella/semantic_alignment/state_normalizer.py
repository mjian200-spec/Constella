from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
from typing import Any

from .models import AlignmentStatus, ConceptType, MatchMethod, ProposalKind
from .registry import ConceptRegistry, normalize_text


_NUMBER = r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)"
_KNOWN_UNITS = (
    "°C", "℃", "K", "mA", "kA", "A", "mV", "kV", "V", "μm", "µm", "mm", "cm", "m",
    "ms", "min", "s", "h", "%",
)
_UNIT = (
    "(?:" + "|".join(re.escape(value) for value in _KNOWN_UNITS)
    + r"|[A-Za-zμµΩ°℃%][A-Za-z0-9μµΩ°℃%·/*().^-]{0,20})"
)
_RANGE = re.compile(
    rf"(?P<low>{_NUMBER})\s*(?:~|～|—|–|至|到)\s*(?P<high>{_NUMBER})\s*(?P<unit>{_UNIT})?",
    re.IGNORECASE,
)
_COMPARE = re.compile(
    rf"(?P<operator>大于等于|小于等于|不低于|不高于|不少于|不超过|超过|大于|高于|低于|小于|等于|≥|≤|>=|<=|>|<|=)\s*"
    rf"(?P<value>{_NUMBER})\s*(?P<unit>{_UNIT})?",
    re.IGNORECASE,
)
_SCALAR = re.compile(rf"(?P<value>{_NUMBER})\s*(?P<unit>{_UNIT})", re.IGNORECASE)

_OPERATOR_MAP = {
    "超过": (">", False), "大于": (">", False), "高于": (">", False), ">": (">", False),
    "大于等于": (">", True), "不低于": (">", True), "不少于": (">", True), "≥": (">", True), ">=": (">", True),
    "低于": ("<", False), "小于": ("<", False), "<": ("<", False),
    "小于等于": ("<", True), "不高于": ("<", True), "不超过": ("<", True), "≤": ("<", True), "<=": ("<", True),
    "等于": ("=", True), "=": ("=", True),
}

_GENERIC_PATTERNS = {
    "增加", "增大", "提高", "上升", "降低", "减小", "下降", "变化", "不变",
    "大", "小", "高", "低", "较大", "较小", "较高", "较低", "很大", "很小", "很高", "很低",
    "增强", "减弱", "改善", "恶化", "形成", "产生", "存在", "无", "有",
}


class StateNormalizer:
    """Deterministic surface and quantity normalization with typed registry lookup."""

    def __init__(self, registry: ConceptRegistry, *, proposal_threshold: int = 5) -> None:
        if proposal_threshold < 1:
            raise ValueError("proposal_threshold must be at least 1")
        self.registry = registry
        self.proposal_threshold = proposal_threshold

    def normalize(
        self,
        raw_state: str,
        *,
        frequency: int,
        raw_object: str = "",
        subject_object_concept_ids: list[str] | None = None,
        qualifiers: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        raw = str(raw_state)
        surface = self._surface(raw)
        quantity_result = self._quantity(surface)
        lookup_values = [surface]
        progressive = self._progressive_form(surface)
        if progressive and normalize_text(progressive) != normalize_text(surface):
            lookup_values.append(progressive)
        resolution = self._resolve(lookup_values)
        canonical_surface = surface
        if resolution["status"] in {AlignmentStatus.MATCHED, AlignmentStatus.TYPE_REVIEW}:
            concept_id = str(resolution["concept_id"])
            canonical_surface = str(self.registry.concepts[concept_id].get("canonical_name") or surface)
        if quantity_result:
            canonical_surface = quantity_result["canonical_surface"]
        result_qualifiers = list(qualifiers or [])
        if quantity_result and quantity_result.get("dimension"):
            dimension = {"dimension": quantity_result["dimension"]}
            if dimension not in result_qualifiers:
                result_qualifiers.append(dimension)
        result = {
            "raw_state": raw,
            "canonical_surface": canonical_surface,
            "state_concept_id": resolution.get("concept_id"),
            "state_candidates": resolution.get("candidates", []),
            "match_method": resolution.get("match_method", MatchMethod.NONE),
            "operator_family": quantity_result.get("operator_family") if quantity_result else None,
            "quantity": quantity_result.get("quantity") if quantity_result else None,
            "qualifiers": result_qualifiers,
            "alignment_status": resolution["status"],
            "proposal": None,
        }
        if resolution["status"] == AlignmentStatus.TYPE_REVIEW:
            result["proposal"] = {
                "proposal_kind": ProposalKind.TYPE_REVIEW,
                "concept_type": ConceptType.STATE,
                "concept_id": resolution["concept_id"],
                "canonical_name": canonical_surface,
            }
        elif resolution["status"] == AlignmentStatus.EXPRESSION_ONLY and frequency >= self.proposal_threshold:
            proposal_kind = self._proposal_kind(surface, quantity_result)
            proposal_name = self._proposal_name(surface, quantity_result)
            if proposal_name:
                result["alignment_status"] = AlignmentStatus.PROPOSED
                result["proposal"] = {
                    "proposal_kind": proposal_kind,
                    "concept_type": ConceptType.STATE,
                    "canonical_name": proposal_name,
                    "raw_object": raw_object,
                }
        return result

    def _resolve(self, values: list[str]) -> dict[str, Any]:
        ambiguous: list[dict[str, Any]] = []
        for value in values:
            resolved = self.registry.resolve_exact(value, concept_type=ConceptType.STATE)
            if resolved["status"] in {AlignmentStatus.MATCHED, AlignmentStatus.TYPE_REVIEW}:
                return resolved
            if resolved["status"] == AlignmentStatus.AMBIGUOUS:
                ambiguous.extend(resolved["candidates"])
        if ambiguous:
            unique = {row["id"]: row for row in ambiguous}
            return {
                "status": AlignmentStatus.AMBIGUOUS,
                "concept_id": None,
                "match_method": MatchMethod.NONE,
                "candidates": list(unique.values()),
            }
        return {
            "status": AlignmentStatus.EXPRESSION_ONLY,
            "concept_id": None,
            "match_method": MatchMethod.NONE,
            "candidates": [],
        }

    @staticmethod
    def _surface(value: str) -> str:
        text = re.sub(r"\s+", " ", str(value)).strip(" \t。；;")
        text = re.sub(r"^(?:处于|呈现?|正处于)\s*", "", text)
        if len(text) > 2:
            text = re.sub(r"(?:的)?状态$", "", text).strip()
        return text

    @staticmethod
    def _progressive_form(value: str) -> str | None:
        match = re.fullmatch(r"正在(.{1,8})", value)
        return f"{match.group(1)}中" if match else None

    def _quantity(self, surface: str) -> dict[str, Any] | None:
        for kind, pattern in (("range", _RANGE), ("compare", _COMPARE), ("scalar", _SCALAR)):
            match = pattern.search(surface)
            if not match:
                continue
            prefix = surface[:match.start()].strip(" （([")
            suffix = surface[match.end():].strip(" ）)]时")
            dimension = self._dimension(prefix, suffix)
            unit = str(match.groupdict().get("unit") or "")
            if kind == "range":
                low = self._convert(match.group("low"), unit)
                high = self._convert(match.group("high"), unit)
                canonical_unit = low[1] if low else unit or None
                quantity = {
                    "lower": low[0] if low else match.group("low"),
                    "upper": high[0] if high else match.group("high"),
                    "unit_original": unit or None,
                    "unit_canonical": canonical_unit,
                    "precision": max(
                        self._precision(match.group("low")), self._precision(match.group("high")),
                        self._precision(low[0]) if low else 0,
                        self._precision(high[0]) if high else 0,
                    ),
                    "inclusive": True,
                    "conversion_status": "CONVERTED" if unit and canonical_unit != unit else "UNCHANGED",
                }
                return {
                    "operator_family": "BETWEEN",
                    "quantity": quantity,
                    "dimension": dimension,
                    "canonical_surface": f"{dimension or ''}BETWEEN{{quantity}}",
                }
            converted = self._convert(match.group("value"), unit)
            canonical_value, canonical_unit = converted if converted else (match.group("value"), unit or None)
            if kind == "compare":
                operator_family, inclusive = _OPERATOR_MAP[match.group("operator")]
            else:
                operator_family, inclusive = "=", True
            quantity = {
                "value": canonical_value,
                "unit_original": unit or None,
                "unit_canonical": canonical_unit,
                "precision": max(
                    self._precision(match.group("value")), self._precision(canonical_value),
                ),
                "inclusive": inclusive,
                "conversion_status": "CONVERTED" if unit and canonical_unit != unit else "UNCHANGED",
            }
            return {
                "operator_family": operator_family,
                "quantity": quantity,
                "dimension": dimension,
                "canonical_surface": f"{dimension or ''}{operator_family}{{quantity}}",
            }
        return None

    @staticmethod
    def _dimension(prefix: str, suffix: str) -> str | None:
        value = re.sub(r"^(?:当|在)", "", prefix).strip()
        if value and len(value) <= 20 and not re.search(r"[<>≥≤=]", value):
            return value
        value = suffix.strip()
        return value if value and len(value) <= 20 else None

    @staticmethod
    def _precision(value: str) -> int:
        return len(value.partition(".")[2])

    @staticmethod
    def _decimal_text(value: Decimal) -> str:
        normalized = format(value.normalize(), "f")
        return "0" if normalized in {"-0", ""} else normalized

    def _convert(self, value: str, unit: str) -> tuple[str, str | None] | None:
        try:
            number = Decimal(value)
        except InvalidOperation:
            return None
        if not unit:
            return self._decimal_text(number), None
        conversions: dict[str, tuple[Decimal, Decimal, str]] = {
            "°C": (Decimal("1"), Decimal("273.15"), "K"),
            "℃": (Decimal("1"), Decimal("273.15"), "K"),
            "K": (Decimal("1"), Decimal("0"), "K"),
            "mA": (Decimal("0.001"), Decimal("0"), "A"),
            "kA": (Decimal("1000"), Decimal("0"), "A"),
            "A": (Decimal("1"), Decimal("0"), "A"),
            "mV": (Decimal("0.001"), Decimal("0"), "V"),
            "kV": (Decimal("1000"), Decimal("0"), "V"),
            "V": (Decimal("1"), Decimal("0"), "V"),
            "μm": (Decimal("0.001"), Decimal("0"), "mm"),
            "µm": (Decimal("0.001"), Decimal("0"), "mm"),
            "mm": (Decimal("1"), Decimal("0"), "mm"),
            "cm": (Decimal("10"), Decimal("0"), "mm"),
            "m": (Decimal("1000"), Decimal("0"), "mm"),
            "ms": (Decimal("0.001"), Decimal("0"), "s"),
            "s": (Decimal("1"), Decimal("0"), "s"),
            "min": (Decimal("60"), Decimal("0"), "s"),
            "h": (Decimal("3600"), Decimal("0"), "s"),
            "%": (Decimal("1"), Decimal("0"), "%"),
        }
        if unit not in conversions:
            return self._decimal_text(number), unit
        factor, offset, canonical_unit = conversions[unit]
        return self._decimal_text(number * factor + offset), canonical_unit

    @staticmethod
    def _proposal_kind(surface: str, quantity: dict[str, Any] | None) -> str:
        if quantity or normalize_text(surface) in {normalize_text(value) for value in _GENERIC_PATTERNS}:
            return ProposalKind.NORMALIZATION_PATTERN
        return ProposalKind.STATE_CONCEPT

    @staticmethod
    def _proposal_name(surface: str, quantity: dict[str, Any] | None) -> str:
        if quantity:
            return str(quantity["canonical_surface"])
        return surface
