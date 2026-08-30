from __future__ import annotations

from constella.semantic_alignment.models import AlignmentStatus, ProposalKind
from constella.semantic_alignment.registry import ConceptRegistry, MemorySnapshot
from constella.semantic_alignment.state_normalizer import StateNormalizer


def _normalizer() -> StateNormalizer:
    memory = MemorySnapshot.build([
        {"concept_id": "charging", "canonical_name": "充电中", "aliases": ["正在充电"], "type": "state", "registration_status": "APPROVED"},
        {"concept_id": "increase", "canonical_name": "增大", "aliases": ["提高"], "type": "state", "registration_status": "APPROVED"},
        {"concept_id": "untyped", "canonical_name": "短路过渡", "aliases": []},
    ], [])
    return StateNormalizer(ConceptRegistry(memory), proposal_threshold=1)


def test_temperature_conversion_is_numeric_not_unit_relabeling():
    row = _normalizer().normalize("温度超过60°C", frequency=1, raw_object="电池组")
    assert row["operator_family"] == ">"
    assert row["quantity"]["value"] == "333.15"
    assert row["quantity"]["unit_original"] == "°C"
    assert row["quantity"]["unit_canonical"] == "K"
    assert row["quantity"]["precision"] == 2
    assert row["quantity"]["inclusive"] is False
    assert row["qualifiers"] == [{"dimension": "温度"}]


def test_inclusive_comparison_is_preserved():
    row = _normalizer().normalize("≥120A", frequency=1)
    assert row["operator_family"] == ">"
    assert row["quantity"]["value"] == "120"
    assert row["quantity"]["inclusive"] is True


def test_range_is_parameterized_without_becoming_concept_identity():
    row = _normalizer().normalize("2~3.5", frequency=1)
    assert row["operator_family"] == "BETWEEN"
    assert row["quantity"]["lower"] == "2"
    assert row["quantity"]["upper"] == "3.5"
    assert row["proposal"]["proposal_kind"] == ProposalKind.NORMALIZATION_PATTERN


def test_unknown_ascii_unit_is_preserved_without_guessing_conversion():
    row = _normalizer().normalize("大于12foo", frequency=1)
    assert row["quantity"]["value"] == "12"
    assert row["quantity"]["unit_original"] == "foo"
    assert row["quantity"]["unit_canonical"] == "foo"
    assert row["quantity"]["conversion_status"] == "UNCHANGED"


def test_reviewed_alias_normalizes_surface_to_state_concept():
    row = _normalizer().normalize("正在充电", frequency=1)
    assert row["alignment_status"] == AlignmentStatus.MATCHED
    assert row["state_concept_id"] == "charging"
    assert row["canonical_surface"] == "充电中"


def test_generic_change_expression_is_normalization_pattern_not_concept():
    row = _normalizer().normalize("增加", frequency=1)
    assert row["alignment_status"] == AlignmentStatus.PROPOSED
    assert row["proposal"]["proposal_kind"] == ProposalKind.NORMALIZATION_PATTERN


def test_unregistered_state_match_requires_concept_admission():
    row = _normalizer().normalize("短路过渡", frequency=1)
    assert row["alignment_status"] == AlignmentStatus.PROPOSED
    assert row["state_concept_id"] is None
    assert row["candidate_concept_id"] == "untyped"
    assert row["proposal"]["proposal_kind"] == ProposalKind.CONCEPT_APPROVAL


def test_ms_and_min_units_win_over_bare_m():
    row = _normalizer().normalize("保温3min", frequency=1)
    assert row["quantity"]["unit_original"] == "min"
    assert row["quantity"]["unit_canonical"] == "s"
    assert row["quantity"]["value"] == "180"
    row = _normalizer().normalize("延迟40ms", frequency=1)
    assert row["quantity"]["unit_original"] == "ms"
    assert row["quantity"]["value"] == "0.04"


def test_negated_comparators_keep_their_meaning():
    row = _normalizer().normalize("充电电流不大于60A", frequency=1)
    assert row["operator_family"] == "<"
    assert row["quantity"]["inclusive"] is True
    row = _normalizer().normalize("不小于60A", frequency=1)
    assert row["operator_family"] == ">"
    assert row["quantity"]["inclusive"] is True


def test_range_with_unit_on_low_endpoint_keeps_both_bounds():
    row = _normalizer().normalize("加入1%~5% O2", frequency=1)
    assert row["operator_family"] == "BETWEEN"
    assert row["quantity"]["lower"] == "1"
    assert row["quantity"]["upper"] == "5"
    assert row["quantity"]["unit_original"] == "%"


def test_scientific_notation_magnitude_is_kept():
    row = _normalizer().normalize("大于1e3A", frequency=1)
    assert row["quantity"]["value"] == "1000"
    assert row["quantity"]["unit_original"] == "A"
