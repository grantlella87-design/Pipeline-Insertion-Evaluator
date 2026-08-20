"""Tests for GSEP eligibility.

These run anywhere: no network, no ArcGIS token, no GIS install. They pin the
rule the candidate list depends on.

The last class is the one that matters most: it checks that `is_eligible` and
`where_clause` describe the same rule. Two expressions of one rule is a
deliberate choice - the workflow filters locally, a published layer filters in
SQL - and the risk it carries is that a threshold moves in one and not the
other.
"""
import re

import pytest

from pipelineinsertion import config, gsep
from pipelineinsertion.fields import iso_date_to_epoch_ms

BEFORE_CUTOFF = iso_date_to_epoch_ms("1960-06-15")
ON_CUTOFF = iso_date_to_epoch_ms("1971-08-01")
AFTER_CUTOFF = iso_date_to_epoch_ms("1990-01-01")


class TestEligibleMaterials:
    @pytest.mark.parametrize("assettype,reason", [
        (config.ASSETTYPE_BARE_STEEL, gsep.REASON_BARE_STEEL),
        (config.ASSETTYPE_COPPER, gsep.REASON_COPPER),
        (config.ASSETTYPE_WROUGHT_IRON, gsep.REASON_WROUGHT_IRON),
    ])
    def test_eligible_regardless_of_diameter_or_date(self, assettype, reason):
        # These three have no further test to pass, so a missing diameter and a
        # missing date must not exclude them.
        assert gsep.eligibility(assettype, None, None) == (True, reason)
        assert gsep.eligibility(assettype, 48, AFTER_CUTOFF) == (True, reason)

    def test_unlisted_material_is_not_eligible(self):
        eligible, reason = gsep.eligibility(99, 4, BEFORE_CUTOFF)
        assert eligible is False
        assert reason == gsep.REASON_INELIGIBLE_MATERIAL

    def test_missing_assettype_is_not_eligible(self):
        for value in (None, "", "   ", float("nan")):
            assert gsep.eligibility(value) == (False, gsep.REASON_NO_ASSETTYPE)

    def test_float_codes_decode(self):
        # pandas turns an integer column with a null in it into floats, so 2.0
        # has to mean cast iron.
        assert gsep.is_eligible(2.0, 8) is True
        assert gsep.is_eligible("2", 8) is True


class TestCastIron:
    @pytest.mark.parametrize("diameter,expected", [
        (4, True), (14, True), (14.0, True),
        (14.5, False), (16, False), (24, False),
    ])
    def test_diameter_boundary_is_inclusive(self, diameter, expected):
        assert gsep.is_eligible(config.ASSETTYPE_CAST_IRON, diameter) is expected

    def test_reason_distinguishes_too_large_from_missing(self):
        assert gsep.eligibility(config.ASSETTYPE_CAST_IRON, 16)[1] == (
            gsep.REASON_CAST_IRON_TOO_LARGE)
        assert gsep.eligibility(config.ASSETTYPE_CAST_IRON, None)[1] == (
            gsep.REASON_CAST_IRON_NO_DIAMETER)

    def test_diameter_with_units_parses(self):
        assert gsep.is_eligible(config.ASSETTYPE_CAST_IRON, '8"') is True
        assert gsep.is_eligible(config.ASSETTYPE_CAST_IRON, "16 IN") is False

    def test_threshold_comes_from_config(self, restore_config):
        assert gsep.is_eligible(config.ASSETTYPE_CAST_IRON, 16) is False
        restore_config("CAST_IRON_MAX_DIAMETER_IN", 24.0)
        assert gsep.is_eligible(config.ASSETTYPE_CAST_IRON, 16) is True


class TestCoatedSteel:
    def test_installed_before_cutoff_is_eligible(self):
        assert gsep.eligibility(config.ASSETTYPE_COATED_STEEL, 6, BEFORE_CUTOFF) == (
            True, gsep.REASON_COATED_STEEL)

    def test_cutoff_is_exclusive(self):
        # "installed before 1971-08-01": a main installed on the day itself is
        # not before it.
        eligible, reason = gsep.eligibility(
            config.ASSETTYPE_COATED_STEEL, 6, ON_CUTOFF)
        assert eligible is False
        assert reason == gsep.REASON_COATED_STEEL_TOO_NEW

    def test_installed_after_cutoff_is_not_eligible(self):
        assert gsep.is_eligible(
            config.ASSETTYPE_COATED_STEEL, 6, AFTER_CUTOFF) is False

    def test_missing_date_is_excluded_and_says_so(self):
        eligible, reason = gsep.eligibility(config.ASSETTYPE_COATED_STEEL, 6, None)
        assert eligible is False
        assert reason == gsep.REASON_COATED_STEEL_NO_DATE

    @pytest.mark.parametrize("value", [
        "1960-06-15", "1960-06-15T00:00:00Z", BEFORE_CUTOFF, float(BEFORE_CUTOFF),
    ])
    def test_date_accepted_in_the_forms_it_arrives_in(self, value):
        # Epoch ms from the service, a string from a hand-edited export, a float
        # after a pandas round-trip.
        assert gsep.is_eligible(config.ASSETTYPE_COATED_STEEL, 6, value) is True

    def test_cutoff_comes_from_config(self, restore_config):
        assert gsep.is_eligible(
            config.ASSETTYPE_COATED_STEEL, 6, AFTER_CUTOFF) is False
        restore_config("COATED_STEEL_INSTALLED_BEFORE", "2000-01-01")
        assert gsep.is_eligible(
            config.ASSETTYPE_COATED_STEEL, 6, AFTER_CUTOFF) is True


class TestPlastic:
    def test_plastic_is_pending_by_default(self):
        # The README leaves plastic open until the GSEP program's ASSETTYPE
        # values are confirmed. Until then no plastic code is claimed.
        assert config.PLASTIC_ASSETTYPES == ()
        assert gsep.plastic_is_pending() is True

    def test_configured_plastic_codes_become_eligible(self, restore_config):
        assert gsep.is_eligible(41, 6) is False
        restore_config("PLASTIC_ASSETTYPES", (41, 42))
        assert gsep.eligibility(41, 6) == (True, gsep.REASON_PLASTIC)
        assert gsep.plastic_is_pending() is False


class TestMaterialLabel:
    def test_known_codes_are_named(self):
        assert gsep.material_label(config.ASSETTYPE_CAST_IRON) == "Cast Iron"
        assert gsep.material_label(config.ASSETTYPE_BARE_STEEL) == "Bare Steel"

    def test_unknown_code_is_blank_rather_than_guessed(self):
        assert gsep.material_label(99) == ""
        assert gsep.material_label(None) == ""


class TestWhereClauseMatchesTheLocalRule:
    """The SQL and the local rule must describe the same thing."""

    def test_clause_names_every_eligible_code(self):
        clause = gsep.where_clause()
        for code in (config.ASSETTYPE_BARE_STEEL, config.ASSETTYPE_CAST_IRON,
                     config.ASSETTYPE_COATED_STEEL, config.ASSETTYPE_COPPER,
                     config.ASSETTYPE_WROUGHT_IRON):
            assert f"ASSETTYPE = {code}" in clause

    def test_clause_carries_the_configured_thresholds(self):
        clause = gsep.where_clause()
        assert f"nominaldiameter <= {config.CAST_IRON_MAX_DIAMETER_IN:g}" in clause
        assert f"DATE '{config.COATED_STEEL_INSTALLED_BEFORE}'" in clause

    def test_clause_follows_config(self, restore_config):
        restore_config("CAST_IRON_MAX_DIAMETER_IN", 24.0)
        restore_config("COATED_STEEL_INSTALLED_BEFORE", "1980-01-01")
        clause = gsep.where_clause()
        assert "nominaldiameter <= 24" in clause
        assert "DATE '1980-01-01'" in clause

    def test_clause_uses_the_field_names_it_is_given(self):
        clause = gsep.where_clause(assettype_field="AT", diameter_field="DIA",
                                   installed_field="INST")
        assert "AT = 1" in clause and "DIA <=" in clause and "INST <" in clause
        assert "ASSETTYPE" not in clause

    def test_configured_plastic_codes_reach_the_sql(self, restore_config):
        restore_config("PLASTIC_ASSETTYPES", (41,))
        assert "ASSETTYPE = 41" in gsep.where_clause()

    def test_clause_matches_the_readme(self):
        # The README states the production query. Whitespace differs; the
        # predicates must not.
        expected = [
            "(ASSETTYPE = 2 AND nominaldiameter <= 14)",
            "(ASSETTYPE = 1)",
            "(ASSETTYPE = 3 AND installationdate < DATE '1971-08-01')",
            "(ASSETTYPE = 5)",
            "(ASSETTYPE = 12)",
        ]
        clause = re.sub(r"\s+", " ", gsep.where_clause())
        for predicate in expected:
            assert predicate in clause
