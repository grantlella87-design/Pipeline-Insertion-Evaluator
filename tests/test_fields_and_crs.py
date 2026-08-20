"""Tests for value cleaning, field-name resolution and the analysis CRS.

The CRS class is the one that matters. This workflow's answer is a distance
threshold, so running the analysis in the wrong units does not fail - it
returns a different set of candidates, and nothing in the output says so.
"""
import datetime as dt

import pytest

from pipelineinsertion import config, crs, fields


class TestClean:
    @pytest.mark.parametrize("value,expected", [
        (None, ""), ("", ""), ("   ", ""),
        ("none", ""), ("NULL", ""), ("nan", ""), ("<Null>", ""), ("N/A", ""),
        ("  Cast   Iron  ", "Cast Iron"),
        ("STEEL", "STEEL"), (0, "0"), (float("nan"), ""),
    ])
    def test_clean(self, value, expected):
        assert fields.clean(value) == expected

    def test_zero_survives(self):
        # A pressure of 0 is a pressure. Treating it as missing loses real mains.
        assert fields.clean(0) == "0"


class TestNormalizeKey:
    @pytest.mark.parametrize("value,expected", [
        ("{ABC}", "ABC"), ("abc", "ABC"), ("  {abc}  ", "ABC"),
        ("123.0", "123"), ("12.5", "12.5"), (None, ""),
    ])
    def test_normalize_key(self, value, expected):
        assert fields.normalize_key(value) == expected

    def test_braced_and_bare_guids_are_the_same_key(self):
        # One endpoint braces them and another does not; joining on the raw
        # values matched nothing.
        assert fields.normalize_key("{ABC}") == fields.normalize_key("abc")

    def test_pandas_float_coercion_is_undone(self):
        # An integer legacy id becomes 123.0 once pandas has seen a null in the
        # column.
        assert fields.normalize_key(123.0) == fields.normalize_key(123)


class TestParseNumber:
    @pytest.mark.parametrize("value,expected", [
        (4, 4.0), (4.5, 4.5), ("4", 4.0), ('4"', 4.0), ("4 IN", 4.0),
        ("-2.5", -2.5), (None, None), ("", None), ("abc", None),
        (True, None), (float("nan"), None), (float("inf"), None),
    ])
    def test_parse_number(self, value, expected):
        assert fields.parse_number(value) == expected

    def test_booleans_are_not_numbers(self):
        # True is 1 in Python. A boolean in a diameter column is bad data, not
        # a one-inch main.
        assert fields.parse_number(True) is None


class TestResolveFieldName:
    def test_returns_the_layers_own_spelling(self):
        available = ["OBJECTID", "operatingpressure", "GlobalID"]
        assert fields.resolve_field_name(available, ["OPERATINGPRESSURE"]) == (
            "operatingpressure")

    def test_ignores_case_and_punctuation(self):
        assert fields.resolve_field_name(["Operating_Pressure"],
                                         ["operatingpressure"]) == "Operating_Pressure"

    def test_first_candidate_wins(self):
        available = ["outsidediameter", "nominaldiameter"]
        assert fields.resolve_field_name(
            available, ["nominaldiameter", "outsidediameter"]) == "nominaldiameter"

    def test_nothing_matched_is_none(self):
        assert fields.resolve_field_name(["A", "B"], ["C"]) is None


class TestDates:
    def test_epoch_ms_passes_through(self):
        assert fields.to_epoch_ms(1000) == 1000

    def test_iso_string_parses(self):
        assert fields.to_epoch_ms("1970-01-01") == 0

    def test_datetime_parses(self):
        when = dt.datetime(1970, 1, 2, tzinfo=dt.timezone.utc)
        assert fields.to_epoch_ms(when) == 86400000

    def test_naive_datetime_is_read_as_utc(self):
        assert fields.to_epoch_ms(dt.datetime(1970, 1, 2)) == 86400000

    def test_date_parses(self):
        assert fields.to_epoch_ms(dt.date(1970, 1, 2)) == 86400000

    def test_missing_is_none(self):
        for value in (None, "", float("nan"), "not a date"):
            assert fields.to_epoch_ms(value) is None

    def test_iso_cutoff_is_utc_midnight(self):
        # A plain date compared in local time moves the boundary by a day for
        # anyone east of Greenwich.
        assert fields.iso_date_to_epoch_ms("1970-01-01") == 0
        assert fields.iso_date_to_epoch_ms("1971-08-01") == 49852800000

    def test_round_trip(self):
        assert fields.epoch_ms_to_iso(fields.iso_date_to_epoch_ms("1971-08-01")) == (
            "1971-08-01")

    def test_epoch_ms_to_iso_handles_missing(self):
        assert fields.epoch_ms_to_iso(None) == ""


class TestAnalysisCrs:
    def test_a_foot_based_projected_crs_is_kept(self):
        # EPSG:2249 is NAD83 / Massachusetts Mainland (ftUS). Reprojecting it
        # would be work for nothing, and not lossless.
        assert crs.is_foot_based("EPSG:2249") is True
        assert crs.analysis_crs("EPSG:2249").to_epsg() == 2249

    def test_a_geographic_crs_is_replaced(self):
        """The failure that does not raise.

        WGS 84 measures in degrees. `geometry.distance` returns a number
        without complaint, and 50 feet is about 0.00014 degrees at this
        latitude - so a 50 ft threshold would accept every pair of systems in
        the state.
        """
        assert crs.is_foot_based("EPSG:4326") is False
        assert crs.analysis_crs("EPSG:4326").to_epsg() == config.FALLBACK_ANALYSIS_EPSG

    def test_a_metre_based_projected_crs_is_replaced(self):
        # Web Mercator measures in metres, so a 50 ft filter would silently
        # become a 164 ft one.
        assert crs.is_foot_based("EPSG:3857") is False
        assert crs.analysis_crs("EPSG:3857").to_epsg() == config.FALLBACK_ANALYSIS_EPSG

    def test_no_crs_at_all_falls_back(self):
        assert crs.is_foot_based(None) is False
        assert crs.analysis_crs(None).to_epsg() == config.FALLBACK_ANALYSIS_EPSG

    def test_unusable_crs_does_not_raise(self):
        assert crs.is_foot_based("not a crs") is False

    def test_fallback_zone_comes_from_config(self, restore_config):
        restore_config("FALLBACK_ANALYSIS_EPSG", 2250)  # MA Island zone
        assert crs.analysis_crs("EPSG:4326").to_epsg() == 2250

    def test_the_fallback_zone_is_itself_foot_based(self):
        # A fallback that does not measure in feet would defeat the point.
        assert crs.is_foot_based(f"EPSG:{config.FALLBACK_ANALYSIS_EPSG}") is True


class TestToAnalysisCrs:
    def test_reprojects_when_needed(self):
        import geopandas as gpd
        from shapely.geometry import LineString

        gdf = gpd.GeoDataFrame(
            {"a": [1]}, geometry=[LineString([(-71.1, 42.3), (-71.0, 42.4)])],
            crs="EPSG:4326")
        result = crs.to_analysis_crs(gdf, "EPSG:2249", "test")
        assert result.crs.to_epsg() == 2249
        # A tenth of a degree is several thousand feet, so the projected
        # geometry must not still be in degree-sized numbers.
        assert result.geometry.iloc[0].length > 1000

    def test_no_reprojection_when_already_there(self):
        import geopandas as gpd
        from shapely.geometry import LineString

        gdf = gpd.GeoDataFrame(
            {"a": [1]}, geometry=[LineString([(0, 0), (10, 0)])], crs="EPSG:2249")
        result = crs.to_analysis_crs(gdf, "EPSG:2249", "test")
        assert result is gdf

    def test_empty_frame_passes_through(self):
        assert crs.to_analysis_crs(None, "EPSG:2249", "test") is None
