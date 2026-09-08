"""The minimum-diameter rule, and the pressure carve-out at the minimum itself.

Insertion threads a new plastic carrier pipe inside the existing main, so below
a certain bore there is no room for one. A main at exactly the minimum is still
acceptable when it runs above 2 PSI: pressure buys back the capacity the
narrower carrier gives up. Below the minimum, no pressure helps.

The class that matters most is TestItIsNotGsepEligibility. The two are
deliberately separate concepts, and folding them together would silently change
what "GSEP eligible" reports across every layer of the output.
"""
import pytest
from shapely.geometry import LineString

from pipelineinsertion import (
    classify,
    config,
    gsep,
    insertability,
    pressure,
    schema,
    systems,
)

WC = config.PRESSURE_UNIT_WC
PSI = config.PRESSURE_UNIT_PSI
INSTALLED_1960 = -315619200000

# Lower Pressure runs to 60" WC, which is 2.17 PSI, so there is a band inside
# the bucket that is above the 2 PSI carve-out threshold. 2 PSI is 55.4" WC.
WC_ABOVE_2_PSI = 58.0
WC_BELOW_2_PSI = 30.0

RESOLVED = {
    "globalid": "GLOBALID", "legacyid": "legacyid", "assetgroup": "ASSETGROUP",
    "assettype": "ASSETTYPE", "diameter": "nominaldiameter",
    "installed": "installationdate", "pressure": "OPERATINGPRESSURE",
    "pressure_units": "pressureunits", "maop": "MAOPRECORD",
    "cpsubnetwork": "cpsubnetworkname",
}


class TestTheThreshold:
    @pytest.mark.parametrize("diameter", [0.5, 1, 2, 3, 3.9])
    @pytest.mark.parametrize("psi", [None, 0.5, 2, 60])
    def test_under_the_minimum_no_pressure_helps(self, diameter, psi):
        # The carve-out is for the minimum itself. Smaller pipe has no room for
        # a carrier at any pressure.
        assert insertability.is_insertable(diameter, psi) is False
        assert insertability.insertability(diameter, psi)[1] == (
            insertability.REASON_TOO_SMALL)

    @pytest.mark.parametrize("diameter", [4.01, 4.5, 6, 8, 12, 24])
    @pytest.mark.parametrize("psi", [None, 0.5, 60])
    def test_over_the_minimum_pressure_does_not_matter(self, diameter, psi):
        assert insertability.is_insertable(diameter, psi) is True
        assert insertability.insertability(diameter, psi)[1] == (
            insertability.REASON_INSERTABLE)

    def test_the_reasons_name_what_excluded_the_main(self):
        for value, expected in (
            (None, insertability.REASON_NO_DIAMETER),
            (3, insertability.REASON_TOO_SMALL),
            (8, insertability.REASON_INSERTABLE),
        ):
            assert insertability.insertability(value)[1] == expected

    def test_a_missing_diameter_is_not_insertable(self):
        """A threshold to test and no value to test it against.

        The same treatment cast iron already gets in gsep.eligibility -
        defaulting either way is a guess. The reason records which value was
        wanted, so a large count reads as a data gap rather than as a fleet of
        small mains.
        """
        for value in (None, "", "   ", float("nan")):
            assert insertability.is_insertable(value, 60) is False
            assert insertability.insertability(value, 60)[1] == (
                insertability.REASON_NO_DIAMETER)

    def test_a_diameter_with_units_parses(self):
        assert insertability.is_insertable('6"') is True
        assert insertability.is_insertable("4 IN", 0.5) is False
        assert insertability.is_insertable("4 IN", 30) is True

    def test_the_threshold_comes_from_config(self, restore_config):
        assert insertability.is_insertable(6) is True
        restore_config("MIN_INSERTION_DIAMETER_IN", 8.0)
        assert insertability.is_insertable(6) is False


class TestTheElevatedPressureCarveOut:
    """4 inch is acceptable where the main's own pressure is above 2 PSI."""

    @pytest.mark.parametrize("psi", [2.01, 5, 15, 60])
    def test_at_the_minimum_and_above_the_pressure_it_is_insertable(self, psi):
        assert insertability.is_insertable(4, psi) is True
        assert insertability.insertability(4, psi)[1] == (
            insertability.REASON_INSERTABLE_AT_ELEVATED)

    @pytest.mark.parametrize("psi", [0, 0.5, 1, 1.99, 2, 2.0])
    def test_at_the_minimum_and_not_above_it_it_is_not(self, psi):
        # "higher than 2 psi" - 2 itself does not qualify.
        assert insertability.is_insertable(4, psi) is False
        assert insertability.insertability(4, psi)[1] == (
            insertability.REASON_AT_MINIMUM_NOT_ELEVATED)

    def test_an_unreadable_pressure_at_the_minimum_excludes_the_main(self):
        """A boundary case with nothing to decide it on.

        Excluded, and the reason names the pressure rather than the diameter,
        so the count reads as a pressure data gap - a different thing to fix
        than a fleet of small mains.
        """
        for value in (None, "", "   ", float("nan")):
            assert insertability.is_insertable(4, value) is False
            assert insertability.insertability(4, value)[1] == (
                insertability.REASON_AT_MINIMUM_NO_PRESSURE)

    def test_the_pressure_threshold_comes_from_config(self, restore_config):
        assert insertability.is_insertable(4, 5) is True
        restore_config("INSERTION_ELEVATED_PRESSURE_PSI", 10.0)
        assert insertability.is_insertable(4, 5) is False
        assert insertability.is_insertable(4, 15) is True

    def test_it_moves_with_the_diameter_threshold(self, restore_config):
        """The carve-out is "at the minimum", not "at 4 inches"."""
        restore_config("MIN_INSERTION_DIAMETER_IN", 6.0)
        assert insertability.is_insertable(4, 60) is False   # now below it
        assert insertability.is_insertable(6, 60) is True    # now at it
        assert insertability.is_insertable(6, 1) is False

    def test_it_is_not_the_lower_pressure_bucket_boundary(self, restore_config):
        """Same number, different rules, and they must not be wired together.

        Widening the pressure bucket should not change which mains can be
        inserted into.
        """
        restore_config("LOWER_PRESSURE_MAX_PSI", 20.0)
        assert insertability.is_insertable(4, 5) is True
        assert insertability.is_insertable(4, 1) is False


class TestTheGeneratedSql:
    def test_it_matches_the_rule(self):
        assert insertability.where_clause() == (
            "(nominaldiameter > 4 OR (nominaldiameter = 4 AND "
            "((pressureunits = 1 AND OPERATINGPRESSURE > 2) OR "
            "(pressureunits = 2 AND OPERATINGPRESSURE > 55.4152))))")

    def test_it_uses_the_field_names_it_is_given(self):
        clause = insertability.where_clause("DIA", "OP", "UNITS")
        assert "DIA > 4" in clause
        assert "UNITS = 1 AND OP > 2" in clause
        assert "nominaldiameter" not in clause

    def test_the_water_column_figure_is_the_psi_threshold_converted(self):
        # A hand-typed WC constant would drift from the PSI one it mirrors.
        expected = config.INSERTION_ELEVATED_PRESSURE_PSI * config.WC_PER_PSI
        assert f"> {expected:g}" in insertability.where_clause()

    def test_it_follows_the_same_config_values_as_the_rule(self, restore_config):
        restore_config("MIN_INSERTION_DIAMETER_IN", 6.0)
        restore_config("INSERTION_ELEVATED_PRESSURE_PSI", 5.0)
        clause = insertability.where_clause()
        assert "nominaldiameter > 6" in clause
        assert "OPERATINGPRESSURE > 5" in clause
        assert f"> {5.0 * config.WC_PER_PSI:g}" in clause

    def test_the_sql_and_the_rule_agree_on_the_boundary_cases(self):
        """The two are separate implementations of one rule; they must match."""
        for diameter, units, recorded, expected in (
            (8, WC, 30, True),            # over the minimum
            (3, PSI, 50, False),          # under it, pressure irrelevant
            (4, WC, WC_ABOVE_2_PSI, True),
            (4, WC, WC_BELOW_2_PSI, False),
            (4, PSI, 5, True),
            (4, PSI, 1, False),
        ):
            psi = pressure.to_psi(recorded, units)
            assert insertability.is_insertable(diameter, psi) is expected
            # And the SQL says the same, read as the service would apply it.
            in_sql = (diameter > config.MIN_INSERTION_DIAMETER_IN
                      or (diameter == config.MIN_INSERTION_DIAMETER_IN
                          and ((units == PSI and recorded > 2)
                               or (units == WC and recorded > 55.4152))))
            assert in_sql is expected


class TestItIsNotGsepEligibility:
    """The two answer different questions and must stay separate.

    GSEP eligibility is about leak-prone material - which mains are worth
    replacing. Insertability is about whether the replacement can be done by
    insertion at all. A 4 inch cast iron main at low pressure is still GSEP
    eligible and still gets replaced; it just is not replaced by insertion.
    """

    def test_a_small_cast_iron_main_is_still_gsep_eligible(self):
        assert gsep.is_eligible(config.ASSETTYPE_CAST_IRON, 4) is True
        assert insertability.is_insertable(4, 1) is False

    def test_gsep_eligibility_does_not_consult_the_insertion_thresholds(
            self, restore_config):
        # Moving either insertion threshold must not move the GSEP counts.
        restore_config("MIN_INSERTION_DIAMETER_IN", 24.0)
        restore_config("INSERTION_ELEVATED_PRESSURE_PSI", 99.0)
        assert gsep.is_eligible(config.ASSETTYPE_CAST_IRON, 8) is True

    def test_the_gsep_sql_says_nothing_about_the_insertion_minimum(self):
        clause = gsep.where_clause()
        assert "> 4" not in clause
        assert "pressureunits" not in clause
        # Its own diameter rule is cast iron's 14 inch ceiling, unrelated.
        assert f"<= {config.CAST_IRON_MAX_DIAMETER_IN:g}" in clause


def make_mains(rows):
    """(assettype, diameter, cp, x_offset[, recorded pressure, units]) mains.

    Laid end to end along y=0, so consecutive rows are contiguous. The default
    pressure is a Lower Pressure water-column figure below the carve-out
    threshold, which is what most of these tests want.
    """
    import geopandas as gpd

    records, geometries = [], []
    for index, row in enumerate(rows):
        assettype, diameter, cp, offset = row[:4]
        recorded, units = (row[4:6] if len(row) > 4 else (WC_BELOW_2_PSI, WC))
        records.append({
            "GLOBALID": "{G%d}" % index, "legacyid": index,
            "ASSETGROUP": 2, "ASSETTYPE": assettype,
            "nominaldiameter": diameter, "installationdate": INSTALLED_1960,
            "OPERATINGPRESSURE": recorded, "pressureunits": units,
            "MAOPRECORD": None, "cpsubnetworkname": cp,
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
            insertability.REASON_INSERTABLE,
            insertability.REASON_AT_MINIMUM_NOT_ELEVATED]

    def test_the_gsep_flag_is_untouched_by_size_or_pressure(self):
        frame = classify.classify(
            make_mains([(config.ASSETTYPE_CAST_IRON, 4, "CP-A", 0)]),
            RESOLVED, layer_json={})
        # Still eligible, just not insertable.
        assert frame.iloc[0][schema.GSEP_ELIGIBLE] == 1
        assert frame.iloc[0][schema.INSERTABLE] == 0

    def test_small_mains_are_kept_out_of_the_insertable_selection(self):
        frame = classify.classify(
            make_mains([(config.ASSETTYPE_CAST_IRON, 8, "CP-A", 0),
                        (config.ASSETTYPE_CAST_IRON, 4, "CP-A", 100),
                        (config.ASSETTYPE_COPPER, 2, "CP-A", 200)]),
            RESOLVED, layer_json={})
        bucket_one = classify.lower_pressure_candidates(frame)
        insertable = classify.insertable_mains(bucket_one)
        assert list(insertable[schema.NOMINAL_DIAMETER]) == [8]

    def test_a_four_inch_main_above_the_pressure_is_insertable(self):
        """The carve-out, end to end and still inside Lower Pressure.

        58" WC is 2.09 PSI: above the 2 PSI carve-out, below the 60" WC ceiling
        on the bucket. That narrow band is where the rule does its work.
        """
        frame = classify.classify(
            make_mains([
                (config.ASSETTYPE_CAST_IRON, 4, "CP-A", 0, WC_ABOVE_2_PSI, WC),
                (config.ASSETTYPE_CAST_IRON, 4, "CP-B", 500, WC_BELOW_2_PSI, WC),
            ]),
            RESOLVED, layer_json={})
        assert list(frame[schema.PRESSURE_BUCKET]) == [config.BUCKET_LOWER] * 2
        insertable = classify.insertable_mains(
            classify.lower_pressure_candidates(frame))
        assert list(insertable[schema.CP_SUBNETWORK]) == ["CP-A"]
        assert list(insertable[schema.INSERTION_REASON]) == [
            insertability.REASON_INSERTABLE_AT_ELEVATED]

    def test_the_pressure_tested_is_the_one_the_bucket_was_decided_on(self):
        """A main whose pressure came from MAOPRECORD is tested on that.

        Otherwise a main could be bucketed on its MAOP fallback and then have
        its bore judged against no pressure at all.
        """
        frame = make_mains([(config.ASSETTYPE_CAST_IRON, 4, "CP-A", 0)])
        frame.loc[0, "OPERATINGPRESSURE"] = None
        frame.loc[0, "MAOPRECORD"] = WC_ABOVE_2_PSI
        classified = classify.classify(frame, RESOLVED, layer_json={})
        assert classified.iloc[0][schema.PRESSURE_FROM_MAOP] == 1
        assert classified.iloc[0][schema.INSERTABLE] == 1

    def test_a_layer_with_no_diameter_field_yields_no_candidates(self):
        """Nothing can be confirmed as insertable without a diameter.

        Better an empty, explained result than a candidate list built on an
        untested rule. Bare steel rather than cast iron, because cast iron
        without a diameter is not GSEP eligible either and the point here is a
        main that is eligible and still cannot be shown to be insertable.
        """
        frame = make_mains([(config.ASSETTYPE_BARE_STEEL, 8, "CP-A", 0)])
        frame = frame.drop(columns=["nominaldiameter"])
        classified = classify.classify(
            frame, dict(RESOLVED, diameter=None), layer_json={})
        bucket_one = classify.lower_pressure_candidates(classified)
        assert len(bucket_one) == 1                       # still GSEP eligible
        assert len(classify.insertable_mains(bucket_one)) == 0
        assert classified.iloc[0][schema.INSERTION_REASON] == (
            insertability.REASON_NO_DIAMETER)


class TestBucketOneKeepsWhatIsNotInsertable:
    """Small pipe is still GSEP eligible. It just is not insertable.

    The bore rule is applied between bucket 1 and the dissolve, so the
    GSEP_LPP_LowerPressure layer keeps the whole GSEP-eligible Lower Pressure
    population. Filtering it there instead would make a layer named and read as
    the GSEP-eligible population mean "GSEP eligible and insertable", and every
    GSEP total drawn from its length would under-report by the small mains.
    """

    @pytest.fixture
    def bucket_one(self):
        frame = classify.classify(
            make_mains([
                (config.ASSETTYPE_CAST_IRON, 8, "CP-A", 0),     # insertable
                (config.ASSETTYPE_CAST_IRON, 4, "CP-A", 100),   # low pressure
                (config.ASSETTYPE_COPPER, 2, "CP-A", 200),      # under the bore
            ]),
            RESOLVED, layer_json={})
        return classify.lower_pressure_candidates(frame)

    def test_the_small_mains_are_still_there(self, bucket_one):
        assert sorted(bucket_one[schema.NOMINAL_DIAMETER]) == [2, 4, 8]

    def test_they_are_all_still_gsep_eligible(self, bucket_one):
        assert list(bucket_one[schema.GSEP_ELIGIBLE]) == [1, 1, 1]

    def test_they_are_flagged_as_not_insertable_rather_than_dropped(
            self, bucket_one):
        not_insertable = bucket_one[bucket_one[schema.INSERTABLE] == 0]
        assert sorted(not_insertable[schema.NOMINAL_DIAMETER]) == [2, 4]

    def test_the_gsep_length_counts_the_small_mains(self, bucket_one):
        """The total the dashboard reports as GSEP-eligible length.

        It is the whole bucket-1 layer's length, so all three mains have to be
        in it - 300 ft, not the 100 ft that is insertable.
        """
        from pipelineinsertion import dashboard_metrics

        totals = dashboard_metrics.gsep_length(
            {schema.GSEP_LOWER_PRESSURE_LAYER: bucket_one})
        assert totals["lower_pressure"] == pytest.approx(300.0)
        insertable = classify.insertable_mains(bucket_one)
        assert dashboard_metrics.geometry_length_ft(insertable) == (
            pytest.approx(100.0))


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
        return systems.dissolve(
            classify.insertable_mains(classify.lower_pressure_candidates(frame)),
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

    def test_the_same_run_above_the_pressure_stays_one_system(self):
        """The pinch is only a pinch below the carve-out pressure.

        The same three mains at 58" WC keep the 4 inch segment, so the run
        dissolves whole rather than splitting.
        """
        frame = classify.classify(
            make_mains([
                (config.ASSETTYPE_CAST_IRON, 8, "CP-A", 0, WC_ABOVE_2_PSI, WC),
                (config.ASSETTYPE_CAST_IRON, 4, "CP-A", 100, WC_ABOVE_2_PSI, WC),
                (config.ASSETTYPE_CAST_IRON, 8, "CP-A", 200, WC_ABOVE_2_PSI, WC),
            ]),
            RESOLVED, layer_json={})
        dissolved = systems.dissolve(
            classify.insertable_mains(classify.lower_pressure_candidates(frame)),
            "GLOBALID", "legacyid")
        assert len(dissolved) == 1
        assert dissolved.iloc[0][schema.MAIN_COUNT] == 3
        assert dissolved.iloc[0][schema.MIN_DIAMETER] == 4
