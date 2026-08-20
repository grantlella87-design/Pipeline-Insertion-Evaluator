"""Tests for pressure classification and unit handling.

The class that matters most is TestCrossUnitComparison. The README states the
final pressure test as a plain numeric comparison, which is correct only when
both sides are recorded in the same unit - and most candidates are in water
column while every target is in PSI. These tests pin the conversion, and the
last one states the defect the conversion exists to avoid.
"""
import pytest

from pipelineinsertion import config, pressure

PSI = config.PRESSURE_UNIT_PSI
WC = config.PRESSURE_UNIT_WC
UNKNOWN = config.PRESSURE_UNIT_UNKNOWN


class TestPressureValue:
    def test_operating_pressure_is_preferred(self):
        assert pressure.pressure_value(30, 99) == 30.0

    def test_maop_is_the_fallback_when_operating_is_null(self):
        assert pressure.pressure_value(None, 45) == 45.0
        assert pressure.pressure_value("", 45) == 45.0

    def test_zero_is_a_pressure_not_a_missing_value(self):
        # COALESCE falls back on NULL, not on a falsy value. A main recorded at
        # 0 must not silently pick up an unrelated MAOP.
        assert pressure.pressure_value(0, 45) == 0.0

    def test_both_missing_is_none(self):
        assert pressure.pressure_value(None, None) is None


class TestUnits:
    @pytest.mark.parametrize("value,expected", [
        (1, PSI), (1.0, PSI), ("1", PSI), (2, WC), (0, UNKNOWN),
    ])
    def test_unit_code(self, value, expected):
        assert pressure.unit_code(value) == expected

    def test_unit_label(self):
        assert pressure.unit_label(PSI) == "PSI"
        assert pressure.unit_label(WC) == "WC"
        assert pressure.unit_label(UNKNOWN) == "Unknown"
        assert pressure.unit_label(None) == ""
        assert pressure.unit_label(7) == ""


class TestToPsi:
    def test_psi_passes_through(self):
        assert pressure.to_psi(25, PSI) == 25.0

    def test_water_column_converts(self):
        assert pressure.to_psi(config.WC_PER_PSI, WC) == pytest.approx(1.0)
        assert pressure.to_psi(14, WC) == pytest.approx(0.5053, abs=1e-4)
        assert pressure.to_psi(60, WC) == pytest.approx(2.1655, abs=1e-4)

    def test_unknown_units_do_not_convert(self):
        # Assuming a unit is how a 55 that meant water column becomes 55 PSI.
        assert pressure.to_psi(55, UNKNOWN) is None
        assert pressure.to_psi(55, None) is None

    def test_missing_value_is_none(self):
        assert pressure.to_psi(None, PSI) is None


class TestLowerPressureBucket:
    @pytest.mark.parametrize("value,units,expected", [
        (0, WC, True), (14, WC, True), (60, WC, True),
        (60.1, WC, False), (100, WC, False),
        (0.5, PSI, True), (2, PSI, True), (2.01, PSI, False), (5, PSI, False),
        (1, UNKNOWN, False),
    ])
    def test_bucket_membership(self, value, units, expected):
        assert pressure.is_lower_pressure(value, units) is expected

    def test_wc_threshold_is_the_catch_all_not_the_classification_boundary(self):
        # 14" WC is where the classification changes; 60" WC is the bucket's
        # catch-all, for systems never re-recorded in PSI.
        assert config.LOWER_PRESSURE_MAX_WC == 60.0
        assert pressure.is_lower_pressure(30, WC) is True

    def test_threshold_comes_from_config(self, restore_config):
        assert pressure.is_lower_pressure(80, WC) is False
        restore_config("LOWER_PRESSURE_MAX_WC", 100.0)
        assert pressure.is_lower_pressure(80, WC) is True


class TestOtherPressureBucket:
    @pytest.mark.parametrize("value,units,expected", [
        (2, PSI, False), (2.01, PSI, True), (60, PSI, True),
        (124, PSI, True), (124.1, PSI, False), (200, PSI, False),
        (100, WC, False), (60, UNKNOWN, False),
    ])
    def test_bucket_membership(self, value, units, expected):
        assert pressure.is_other_pressure(value, units) is expected

    def test_water_column_is_never_an_other_pressure_system(self):
        # A WC value above 2 is a fraction of a PSI, not an elevated system.
        assert pressure.is_other_pressure(1000, WC) is False


class TestBucket:
    def test_buckets_are_named_as_the_readme_names_them(self):
        assert pressure.bucket(30, WC) == "Lower Pressure"
        assert pressure.bucket(20, PSI) == "Other Pressure"

    def test_neither_bucket_is_empty_not_none(self):
        # A value rather than None, so a frame can be grouped and counted by
        # bucket without nulls needing special handling.
        assert pressure.bucket(200, PSI) == ""
        assert pressure.bucket(None, PSI) == ""
        assert pressure.bucket(5, UNKNOWN) == ""

    def test_the_two_buckets_never_overlap(self):
        for units in (PSI, WC, UNKNOWN):
            for value in (0, 1, 2, 2.5, 14, 60, 100, 124, 200):
                both = (pressure.is_lower_pressure(value, units)
                        and pressure.is_other_pressure(value, units))
                assert both is False, f"{value} in {units} is in both buckets"


class TestCrossUnitComparison:
    def test_target_at_or_above_candidate_passes(self):
        assert pressure.target_serves_candidate(2.0, 5.0) is True
        assert pressure.target_serves_candidate(2.0, 2.0) is True

    def test_target_below_candidate_fails(self):
        assert pressure.target_serves_candidate(5.0, 2.0) is False

    def test_unknown_pressure_on_either_side_fails(self):
        assert pressure.target_serves_candidate(None, 5.0) is False
        assert pressure.target_serves_candidate(5.0, None) is False

    def test_water_column_candidate_against_psi_target(self):
        """The case a raw numeric comparison gets wrong.

        A 55" WC candidate is about 2 PSI, so a 5 PSI target really is above it.
        Compared as recorded numbers - 5 >= 55 - the candidate is dropped, and
        because most Lower Pressure systems are recorded in water column, that
        is most of the candidate list.
        """
        candidate = pressure.to_psi(55, WC)
        target = pressure.to_psi(5, PSI)

        # Parenthesised: `5 >= 55 is False` chains into
        # `(5 >= 55) and (55 is False)`, which is False for the wrong reason.
        assert (5 >= 55) is False          # what the raw comparison says
        assert pressure.target_serves_candidate(candidate, target) is True

    def test_a_genuinely_lower_target_still_fails_after_conversion(self):
        # The conversion must not turn the test into one that always passes.
        candidate = pressure.to_psi(2, PSI)
        target = pressure.to_psi(30, WC)  # about 1.08 PSI
        assert pressure.target_serves_candidate(candidate, target) is False


class TestWhereClauses:
    def test_lower_pressure_clause_matches_the_readme(self):
        clause = pressure.lower_pressure_where()
        assert "pressureunits = 2" in clause and "OPERATINGPRESSURE <= 60" in clause
        assert "pressureunits = 1" in clause and "OPERATINGPRESSURE <= 2" in clause

    def test_other_pressure_clause_matches_the_readme(self):
        clause = pressure.other_pressure_where()
        assert "pressureunits = 1" in clause
        assert "OPERATINGPRESSURE > 2" in clause
        assert "OPERATINGPRESSURE <= 124" in clause

    def test_clauses_follow_config(self, restore_config):
        restore_config("OTHER_PRESSURE_MAX_PSI", 99.0)
        assert "OPERATINGPRESSURE <= 99" in pressure.other_pressure_where()

    def test_clauses_use_the_field_names_they_are_given(self):
        clause = pressure.lower_pressure_where(units_field="U", pressure_field="P")
        assert "U = 2" in clause and "P <= 60" in clause
        assert "pressureunits" not in clause
