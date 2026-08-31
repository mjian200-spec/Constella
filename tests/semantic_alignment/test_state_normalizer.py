from __future__ import annotations

from constella.semantic_alignment.state_normalizer import StateNormalizer


def _normalizer() -> StateNormalizer:
    return StateNormalizer()


def test_temperature_conversion_is_numeric_not_unit_relabeling():
    row = _normalizer().normalize("温度超过60°C")
    assert row["operator_family"] == ">"
    assert row["quantity"]["value"] == "333.15"
    assert row["quantity"]["unit_original"] == "°C"
    assert row["quantity"]["unit_canonical"] == "K"
    assert row["quantity"]["precision"] == 2
    assert row["quantity"]["inclusive"] is False
    assert row["qualifiers"] == [{"dimension": "温度"}]


def test_inclusive_comparison_is_preserved():
    row = _normalizer().normalize("≥120A")
    assert row["operator_family"] == ">"
    assert row["quantity"]["value"] == "120"
    assert row["quantity"]["inclusive"] is True


def test_range_is_parameterized_as_expression_not_concept():
    row = _normalizer().normalize("2~3.5")
    assert row["operator_family"] == "BETWEEN"
    assert row["quantity"]["lower"] == "2"
    assert row["quantity"]["upper"] == "3.5"
    assert row["canonical_surface"] == "BETWEEN{quantity}"
    assert "state_concept_id" not in row


def test_unknown_ascii_unit_is_preserved_without_guessing_conversion():
    row = _normalizer().normalize("大于12foo")
    assert row["quantity"]["value"] == "12"
    assert row["quantity"]["unit_original"] == "foo"
    assert row["quantity"]["unit_canonical"] == "foo"
    assert row["quantity"]["conversion_status"] == "UNCHANGED"


def test_surface_is_preserved_without_state_concept_resolution():
    row = _normalizer().normalize("正在充电")
    assert row["canonical_surface"] == "正在充电"
    assert row["operator_family"] is None
    assert row["quantity"] is None


def test_generic_change_expression_stays_an_expression():
    row = _normalizer().normalize("增加")
    assert row["canonical_surface"] == "增加"
    assert row["operator_family"] is None
    assert row["quantity"] is None


def test_unregistered_expression_is_not_proposed_as_state_concept():
    row = _normalizer().normalize("短路过渡")
    assert row["canonical_surface"] == "短路过渡"
    assert row["operator_family"] is None
    assert row["quantity"] is None


def test_ms_and_min_units_win_over_bare_m():
    row = _normalizer().normalize("保温3min")
    assert row["quantity"]["unit_original"] == "min"
    assert row["quantity"]["unit_canonical"] == "s"
    assert row["quantity"]["value"] == "180"
    row = _normalizer().normalize("延迟40ms")
    assert row["quantity"]["unit_original"] == "ms"
    assert row["quantity"]["value"] == "0.04"


def test_negated_comparators_keep_their_meaning():
    row = _normalizer().normalize("充电电流不大于60A")
    assert row["operator_family"] == "<"
    assert row["quantity"]["inclusive"] is True
    row = _normalizer().normalize("不小于60A")
    assert row["operator_family"] == ">"
    assert row["quantity"]["inclusive"] is True


def test_range_with_unit_on_low_endpoint_keeps_both_bounds():
    row = _normalizer().normalize("加入1%~5% O2")
    assert row["operator_family"] == "BETWEEN"
    assert row["quantity"]["lower"] == "1"
    assert row["quantity"]["upper"] == "5"
    assert row["quantity"]["unit_original"] == "%"


def test_scientific_notation_magnitude_is_kept():
    row = _normalizer().normalize("大于1e3A")
    assert row["quantity"]["value"] == "1000"
    assert row["quantity"]["unit_original"] == "A"
