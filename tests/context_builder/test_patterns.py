from pathlib import Path
import unittest

from constella.context_builder.models import SourceRef, Unit
from constella.context_builder.pattern_engine import load_pattern_engine


ROOT = Path(__file__).resolve().parents[2]


class PatternEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = load_pattern_engine(ROOT / "configs/context_builder/patterns.yaml")

    def test_config_is_valid(self):
        self.assertEqual([], self.engine.validate())

    def test_explicit_reference_with_variant_dash(self):
        unit = Unit("p1", "passage", "如图 5—18 所示", SourceRef())
        self.assertEqual("asset_reference.explicit_figure", self.engine.match("asset_reference", unit)[0].pattern_id)

    def test_number_in_measurement_is_not_heading(self):
        unit = Unit("p1", "passage", "焊接电流为5.2 A", SourceRef())
        self.assertEqual([], self.engine.match("heading", unit))
