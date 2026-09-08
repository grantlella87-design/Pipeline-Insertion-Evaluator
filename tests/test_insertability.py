"""The minimum-diameter rule: a main 4 inches or smaller cannot be inserted.

Insertion threads a new plastic carrier pipe inside the existing main, so below
a certain bore there is no room for one.

The class that matters most is TestItIsNotGsepEligibility. The two are
deliberately separate concepts, and folding them together would silently change
what "GSEP eligible" reports across every layer of the output.
"""
import pytest
from shapely.geometry import LineString

from pipelineinsertion import classify, config, gsep, insertability, schema, systems

WC = config.PRESSURE_UNIT_WC
PSI = config.PRESSURE_UNIT_PSI
INSTALLED_1960 = -315619200000

RESOLVED = {
    "globalid": "GLOBALID", "legacyid": "legacyid", "assetgroup": "ASSETGROUP",
    "assettype": "ASSETTYPE", "diameter": "nominaldiameter",
    "installed": "installationdate", "pressure": "OPERATINGPRESSURE",
    "pressure_units": "pressureunits", "maop": "MAOPRECORD",
    "cpsubnetwork": "cpsubnetworkname",
}


class TestTheThreshold:
    @pytest.mark.parametrize("diameter", [0.5, 1, 2, 3, 3.9, 4, 4.0])
    def test_four_inches_and_under_is_not_insertable(self, diameter):
        # "4 inch or less" - the boundary itself is excluded.
        assert insertability.is_insertable(diameter) is False

    @pytest.mark.parametrize("diameter", [4.01, 4.5, 6, 8, 12, 24])
    def test_over_four_inches_is_insertable(self, diameter):
        assert insertability.is_insertable(diameter) is True

    def test_the_reason_distinguishes_too_small_from_unknown(self):
        assert insertability.insertability(4)[1] == (
            insertability.REASON_TOO_SMALL)
        assert insertability.insertability(None)[1] == (
            insertability.REASON_NO_DIAMETER)

    def test_a_missing_diameter_is_not_insertable(self):
        """A threshold to test and no value to test it against.

        The same treatment cast iron already gets in gsep.eligibility -
        defaulting either way is a guess. The reason records which value was
        wanted, so a large count reads as a data gap rather than as a fleet of
        small mains.
        """
        for value in (None, "", "   ", float("nan")):
            assert insertability.is_insertable(value) is False

    def test_a_diameter_with_units_parses(self):
        assert insertability.is_insertable('6"') is True
        assert insertability.is_insertable("4 IN") is False

    def test_the_threshold_comes_from_config(self, restore_config):
        assert insertability.is_insertable(6) is True
        restore_config("MIN_INSERTION_DIAMETER_IN", 8.0)
        assert insertability.is_insertable(6) is False

    def test_the_sql_matches_the_rule(self):
        assert insertability.where_clause() == "(nominaldiameter > 4)"

    def test_the_sql_uses_the_field_name_it_is_given(self):
        assert insertability.where_clause("DIA") == "(DIA > 4)"

    def test_the_sql_follows_the_same_config_value_as_the_rule(self, restore_config):
        # The generated SQL and the local rule must not be able to disagree.
        restore_config("MIN_INSERTION_DIAMETER_IN", 6.0)
        assert insertability.where_clause() == "(nominaldiameter > 6)"
        assert insertability.is_insertable(6) is False
        assert insertability.is_insertable(8) is True


class TestItIsNotGsepEligibility:
    """The two answer different questions and must stay separate.

    GSEP eligibility is about leak-prone material - which mains are worth
    replacing. Insertability is about whether the replacement can be done by
    insertion at all. A 4 inch cast iron main is still GSEP eligible and still
    gets replaced; it just is not replaced by insertion.
    """

    def test_a_small_cast_iron_main_is_still_gsep_eligible(self):
        assert gsep.is_eligible(config.ASSETTYPE_CAST_IRON, 4) is True
        assert insertability.is_insertable(4) is False

    def test_gsep_eligibility_does_not_consult_the_insertion_threshold(
            self, restore_config):
        # Moving the insertion minimum must not move the GSEP counts.
        restore_config("MIN_INSERTION_DIAMETER_IN", 24.0)
        assert gsep.is_eligible(config.ASSETTYPE_CAST_IRON, 8) is True

    def test_the_gsep_sql_says_nothing_about_the_insertion_minimum(self):
        clause = gsep.where_clause()
        assert "> 4" not in clause
        # Its own diameter rule is cast iron's 14 inch ceiling, unrelated.
        assert f"<= {config.CAST_IRON_MAX_DIAMETER_IN:g}" in clause


def make_mains(rows):
    """(assettype, diameter, cp, x_offset) mains, contiguous along y=0."""
    import geopandas as gpd

    records, geometries = [], []
    for index, (assettype, diameter, cp, offset) in enumerate(rows):
        records.append({
            "GLOBALID": "{G%d}" % index, "legacyid": index,
            "ASSETGROUP": 2, "ASSETTYPE": assettype,
            "nominaldiameter": diameter, "installationdate": INSTALLED_1960,
            "OPERATINGPRESSURE": 30, "pressureunits": WC, "MAOPRECORD": None,
            "cpsubnetworkname": cp,
        })
        geometries.append(LineString([(offset, 0), (offset + 100, 0)]))
    return gpd.GeoDataFrame(records, geometry=geometries, crs="EPSG:2249")


class TestClassification:
    def test_both_columns_are_written(self):
        frame = classify.classify(
            make_mains([(config.ASSETTYPE_CAST_IRON, 8, "CP-A", 0),
                        (config.ASSETTYPE_CAST_IRON, 4, "CP-A", 100)]),
            RESOLVED, layer_json={})
        assert list(frame[schema.INSERTABLE]) == [1, 0]
        assert list(frame[schema.INSERTION_REASON]) == [
            insertability.REASON_INSERTABLE, insertability.REASON_TOO_SMALL]

    def test_the_gsep_flag_is_untouched_by_size(self):
        frame = classify.classify(
            make_mains([(config.ASSETTYPE_CAST_IRON, 4, "CP-A", 0)]),
            RESOLVED, layer_json={})
        # Still eligible, just not insertable.
        assert frame.iloc[0][schema.GSEP_ELIGIBLE] == 1
        assert frame.iloc[0][schema.INSERTABLE] == 0

    def test_small_mains_are_kept_out_of_bucket_one(self):
        frame = classify.classify(
            make_mains([(config.ASSETTYPE_CAST_IRON, 8, "CP-A", 0),
                        (config.ASSETTYPE_CAST_IRON, 4, "CP-A", 100),
                        (config.ASSETTYPE_COPPER, 2, "CP-A", 200)]),
            RESOLVED, layer_json={})
        selected = classify.lower_pressure_candidates(frame)
        assert list(selected[schema.NOMINAL_DIAMETER]) == [8]

    def test_a_layer_with_no_diameter_field_yields_no_candidates(self):
        """Nothing can be confirmed as insertable without a diameter.

        Better an empty, explained result than a candidate list built on an
        untested rule.
        """
        frame = make_mains([(config.ASSETTYPE_CAST_IRON, 8, "CP-A", 0)])
        frame = frame.drop(columns=["nominaldiameter"])
        classified = classify.classify(
            frame, dict(RESOLVED, diameter=None), layer_json={})
        assert len(classify.lower_pressure_candidates(classified)) == 0
        assert classified.iloc[0][schema.INSERTION_REASON] == (
            insertability.REASON_NO_DIAMETER)


class TestASmallSegmentSplitsTheSystem:
    """The reason the test is applied per main rather than per system.

    A run of 8 inch mains with one 4 inch segment in the middle cannot be
    inserted *through* that segment, but the 8 inch runs either side still can.
    Dropping the small main and dissolving what is left splits the system
    around it, which is the right answer: excluding the whole system would
    discard insertable main, and keeping it whole would claim a run that cannot
    be threaded end to end.
    """

    @pytest.fixture
    def dissolved(self):
        frame = classify.classify(
            make_mains([
                (config.ASSETTYPE_CAST_IRON, 8, "CP-A", 0),     # insertable
                (config.ASSETTYPE_CAST_IRON, 4, "CP-A", 100),   # the pinch
                (config.ASSETTYPE_CAST_IRON, 8, "CP-A", 200),   # insertable
            ]),
            RESOLVED, layer_json={})
        return systems.dissolve(classify.lower_pressure_candidates(frame),
                                "GLOBALID", "legacyid")

    def test_the_run_becomes_two_systems(self, dissolved):
        assert len(dissolved) == 2

    def test_neither_system_contains_the_small_main(self, dissolved):
        for source_ids in dissolved[schema.SOURCE_IDS]:
            assert "{G1}" not in source_ids       # the 4 inch segment

    def test_the_insertable_main_either_side_is_kept(self, dissolved):
        # Excluding the whole system would have thrown both 8 inch runs away.
        assert sorted(dissolved[schema.MAIN_COUNT]) == [1, 1]
        assert sum(dissolved[schema.LENGTH_FT]) == pytest.approx(200.0)

    def test_no_system_reports_a_diameter_at_or_below_the_minimum(self, dissolved):
        for value in dissolved[schema.MIN_DIAMETER]:
            assert value > config.MIN_INSERTION_DIAMETER_IN
