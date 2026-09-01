from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
from typing import Any


_NUMBER = r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?"
_KNOWN_UNITS = (
    "W/(m²·K)", "W/(m·K)", "g/(A·h)", "g/100g",
    "kg/h", "g/min", "L/min", "m/min", "cm/min", "mm/min", "mm/s",
    "g/cm³", "g/cm3", "W/cm²", "W/cm2", "N/m", "g/L", "g/m",
    "MHz", "kHz", "Hz", "MPa", "kPa", "Pa", "atm",
    "°C", "℃", "K", "mA", "kA", "A", "mV", "kV", "V",
    "μm", "µm", "mm", "cm", "m", "μs", "µs", "ms", "min", "s", "h",
    "μF", "µF", "eV", "J", "W", "N", "L", "g", "°", "%",
)
_KNOWN_UNIT_CASEFOLDS = {value.casefold() for value in _KNOWN_UNITS}
# Known units are case-sensitive and matched longest-first. The lower-case
# fallback deliberately excludes upper-case symbols: it preserves an unknown
# unit such as ``foo`` without turning formulas/grades such as Al2O3, 0.001I,
# or H08Mn into quantities.
_UNIT = (
    "(?:" + "|".join(
        re.escape(value) for value in sorted(_KNOWN_UNITS, key=len, reverse=True)
    )
    + r"|[a-z][a-z0-9·/*().^-]{1,20})"
    + r"(?![A-Za-z0-9μµΩ°℃%])"
)
_RANGE = re.compile(
    rf"(?<![A-Za-z0-9])(?P<low>{_NUMBER})\s*(?P<low_unit>{_UNIT})?\s*"
    rf"(?:~|～|—|–|至|到)\s*(?P<high>{_NUMBER})\s*(?P<unit>{_UNIT})?",
)
_COMPARE = re.compile(
    rf"(?P<operator>不大于|不小于|大于等于|小于等于|不低于|不高于|不少于|不超过|超过|大于|高于|低于|小于|等于|≥|≤|>=|<=|>|<|=)\s*"
    rf"(?P<value>{_NUMBER})\s*(?P<unit>{_UNIT})?",
)
_SCALAR = re.compile(
    rf"(?<![A-Za-z0-9])(?P<value>{_NUMBER})\s*(?P<unit>{_UNIT})",
)
_FORMULA_OR_GRADE = (
    re.compile(r"^(?:[A-Z][a-z]?\d*){2,}(?:\([^)]*\))?(?:-[A-Z])?$"),
    re.compile(r"^[A-Za-z]{1,6}\d{1,4}(?:-\d+|[A-Za-z][A-Za-z0-9]*)$"),
    re.compile(rf"^{_NUMBER}[A-Z][A-Za-z0-9_()/*+.^-]*$"),
)

_OPERATOR_MAP = {
    "超过": (">", False), "大于": (">", False), "高于": (">", False), ">": (">", False),
    "大于等于": (">", True), "不低于": (">", True), "不少于": (">", True), "不小于": (">", True),
    "≥": (">", True), ">=": (">", True),
    "低于": ("<", False), "小于": ("<", False), "<": ("<", False),
    "小于等于": ("<", True), "不高于": ("<", True), "不超过": ("<", True), "不大于": ("<", True),
    "≤": ("<", True), "<=": ("<", True),
    "等于": ("=", True), "=": ("=", True),
}


class StateNormalizer:
    """Deterministic surface and quantity normalization of state expressions.

    State concepts are no longer admitted; every state is expressed
    structurally (canonical_surface + operator_family + quantity + qualifiers)
    against its subject object, never resolved to a concept.
    """

    def normalize(self, raw_state: str) -> dict[str, Any]:
        raw = str(raw_state)
        surface = self._surface(raw)
        quantity_result = self._quantity(surface)
        canonical_surface = (
            quantity_result["canonical_surface"] if quantity_result else surface
        )
        qualifiers: list[dict[str, Any]] = []
        if quantity_result and quantity_result.get("dimension"):
            qualifiers.append({"dimension": quantity_result["dimension"]})
        return {
            "raw_state": raw,
            "canonical_surface": canonical_surface,
            "operator_family": (
                quantity_result["operator_family"] if quantity_result else None
            ),
            "quantity": quantity_result.get("quantity") if quantity_result else None,
            "qualifiers": qualifiers,
        }

    @staticmethod
    def _surface(value: str) -> str:
        text = re.sub(r"\s+", " ", str(value)).strip(" \t。；;")
        text = re.sub(r"^(?:处于|呈现?|正处于)\s*", "", text)
        if len(text) > 2:
            text = re.sub(r"(?:的)?状态$", "", text).strip()
        return text

    def _quantity(self, surface: str) -> dict[str, Any] | None:
        if any(pattern.fullmatch(surface) for pattern in _FORMULA_OR_GRADE):
            return None
        for kind, pattern in (("range", _RANGE), ("compare", _COMPARE), ("scalar", _SCALAR)):
            match = pattern.search(surface)
            if not match:
                continue
            prefix = surface[:match.start()].strip(" （([")
            suffix = surface[match.end():].strip(" ）)]时")
            dimension = self._dimension(prefix, suffix)
            groups = match.groupdict()
            unit = str(groups.get("unit") or "")
            units = [unit, str(groups.get("low_unit") or "")]
            if any(
                value and value not in _KNOWN_UNITS
                and value.casefold() in _KNOWN_UNIT_CASEFOLDS
                for value in units
            ):
                # A known symbol written with the wrong case is not silently
                # reclassified as an arbitrary unknown unit.
                continue
            if kind == "range":
                low_unit = str(groups.get("low_unit") or "") or unit
                low = self._convert(match.group("low"), low_unit)
                high = self._convert(match.group("high"), unit or low_unit)
                canonical_unit = low[1] if low else (unit or low_unit or None)
                converted = bool(
                    (low_unit and low and low[1] != low_unit)
                    or (unit and high and high[1] != unit)
                )
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
                    "conversion_status": "CONVERTED" if converted else "UNCHANGED",
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
            "μs": (Decimal("0.000001"), Decimal("0"), "s"),
            "µs": (Decimal("0.000001"), Decimal("0"), "s"),
            "Hz": (Decimal("1"), Decimal("0"), "Hz"),
            "kHz": (Decimal("1000"), Decimal("0"), "Hz"),
            "MHz": (Decimal("1000000"), Decimal("0"), "Hz"),
            "Pa": (Decimal("1"), Decimal("0"), "Pa"),
            "kPa": (Decimal("1000"), Decimal("0"), "Pa"),
            "MPa": (Decimal("1000000"), Decimal("0"), "Pa"),
            "atm": (Decimal("101325"), Decimal("0"), "Pa"),
            "%": (Decimal("1"), Decimal("0"), "%"),
        }
        if unit not in conversions:
            return self._decimal_text(number), unit
        factor, offset, canonical_unit = conversions[unit]
        return self._decimal_text(number * factor + offset), canonical_unit
