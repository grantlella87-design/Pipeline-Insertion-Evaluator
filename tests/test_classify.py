"""Tests for turning a downloaded Main Lines frame into the two buckets.

The distinction these protect is the one easiest to get wrong: bucket 1 is
filtered on GSEP eligibility and bucket 2 is not. Filtering the targets on GSEP
too would discard most of the elevated network the candidates are meant to be
inserted into, and the only symptom would be a candidate list that is quietly
too short.
"""
import pytest
from shapely.geometry import LineString

from pipelineinsertion import classify, config, schema
from pipelineinsertion.fields import iso_date_to_epoch_ms

RESOLVED = {
    "globalid": "GLOBALID", "legacyid": "legacyid", "assettype": "ASSETTYPE",
    "diameter": "nominaldiameter", "installed": "installationdate",
    "pressure": "OPERATINGPRESSURE", "pressure_units": "pressureunits",
    "maop": "MAOPRECORD",
}

WC = config.PRESSURE_UNIT_WC
PSI = config.PRESSURE_UNIT_PSI
UNKNOWN_UNITS = config.PRESSURE_UNIT_UNKNOWN

PRE_CUTOFF = iso_date_to_epoch_ms("1960-01-01")
POST_CUTOFF = iso_date_to_epoch_ms("1990-01-01")


def make_mains(rows):
    """A raw Main Lines frame.

    Each row is (assettype, diameter, installed, pressure, units, maop).
    """
    import geopandas as gpd

    records = []
    geometries = []
    for index, (assettype, diameter, installed, value, units, maop) in enumerate(rows):
        records.append({
            "GLOBALID": "{G%d}" % index, "legacyid": index,
            "ASSETTYPE": assettype, "nominaldiameter": diameter,
            "installationdate": installed, "OPERATINGPRESSURE": value,
            "pressureunits": units, "MAOPRECORD": maop,
        })
        geometries.append(LineString([(index * 10, 0), (index * 10 + 5, 0)]))
    return gpd.GeoDataFrame(records, geometry=geometries, crs="EPSG:2249")


class TestClassify:
    def test_writes_every_declared_column(self):
        frame = classify.classify(make_mains([(1, 6, None, 30, WC, None)]), RESOLVED)
        for name in (schema.GSEP_ELIGIBLE, schema.GSEP_REASON, schema.MATERIAL,
                     schema.PRESSURE, schema.PRESSURE_UNITS,
                     schema.PRESSURE_UNIT_LABEL, schema.PRESSURE_PSI,
                     schema.PRESSURE_BUCKET, schema.PRESSURE_FROM_MAOP):
            assert name in frame.columns

    def test_does_not_modify_the_input(self):
        source = make_mains([(1, 6, None, 30, WC, None)])
        before = list(source.columns)
        classify.classify(source, RESOLVED)
        assert list(source.columns) == before

    def test_eligibility_and_bucket_per_row(self):
        frame = classify.classify(make_mains([
            (config.ASSETTYPE_BARE_STEEL, 6, None, 30, WC, None),
            (config.ASSETTYPE_CAST_IRON, 16, None, 30, WC, None),
            (config.ASSETTYPE_COATED_STEEL, 6, POST_CUTOFF, 20, PSI, None),
        ]), RESOLVED)
        assert list(frame[schema.GSEP_ELIGIBLE]) == [1, 0, 0]
        assert list(frame[schema.PRESSURE_BUCKET]) == [
            config.BUCKET_LOWER, config.BUCKET_LOWER, config.BUCKET_OTHER]

    def test_material_labels_are_named(self):
        frame = classify.classify(make_mains([
            (config.ASSETTYPE_CAST_IRON, 8, None, 30, WC, None)]), RESOLVED)
        assert frame.iloc[0][schema.MATERIAL] == "Cast Iron"

    def test_domain_labels_override_the_built_in_ones(self):
        # A material renamed on the service should report its current name.
        frame = classify.classify(
            make_mains([(config.ASSETTYPE_CAST_IRON, 8, None, 30, WC, None)]),
            RESOLVED, domain_labels={config.ASSETTYPE_CAST_IRON: "CI Main"})
        assert frame.iloc[0][schema.MATERIAL] == "CI Main"

    def test_maop_fallback_is_used_and_flagged(self):
        frame = classify.classify(
            make_mains([(1, 6, None, None, WC, 30)]), RESOLVED)
        assert frame.iloc[0][schema.PRESSURE] == 30.0
        assert frame.iloc[0][schema.PRESSURE_FROM_MAOP] == 1
        assert frame.iloc[0][schema.PRESSURE_BUCKET] == config.BUCKET_LOWER

    def test_operating_pressure_is_not_flagged_as_a_fallback(self):
        frame = classify.classify(
            make_mains([(1, 6, None, 30, WC, 99)]), RESOLVED)
        assert frame.iloc[0][schema.PRESSURE] == 30.0
        assert frame.iloc[0][schema.PRESSURE_FROM_MAOP] == 0

    def test_pressure_psi_is_computed(self):
        frame = classify.classify(
            make_mains([(1, 6, None, config.WC_PER_PSI, WC, None)]), RESOLVED)
        assert frame.iloc[0][schema.PRESSURE_PSI] == pytest.approx(1.0)

    def test_unknown_units_land_in_neither_bucket(self):
        frame = classify.classify(
            make_mains([(1, 6, None, 5, UNKNOWN_UNITS, None)]), RESOLVED)
        assert frame.iloc[0][schema.PRESSURE_BUCKET] == ""
        assert frame.iloc[0][schema.PRESSURE_PSI] is None

    def test_missing_optional_fields_do_not_raise(self):
        # A layer with no diameter or installation date still classifies; the
        # rules that need them exclude their materials and say why.
        frame = make_mains([(config.ASSETTYPE_CAST_IRON, 8, None, 30, WC, None)])
        frame = frame.drop(columns=["nominaldiameter", "installationdate"])
        resolved = dict(RESOLVED, diameter=None, installed=None)
        result = classify.classify(frame, resolved)
        assert result.iloc[0][schema.GSEP_ELIGIBLE] == 0
        assert result.iloc[0][schema.GSEP_REASON] == "cast_iron_missing_diameter"

    def test_empty_frame(self):
        result = classify.classify(make_mains([]), RESOLVED)
        assert len(result) == 0


class TestBuckets:
    def test_lower_pressure_is_filtered_on_gsep(self):
        frame = classify.classify(make_mains([
            (config.ASSETTYPE_BARE_STEEL, 6, None, 30, WC, None),   # eligible
            (config.ASSETTYPE_CAST_IRON, 24, None, 30, WC, None),   # too large
        ]), RESOLVED)
        selected = classify.lower_pressure_candidates(frame)
        assert len(selected) == 1
        assert selected.iloc[0]["ASSETTYPE"] == config.ASSETTYPE_BARE_STEEL

    def test_other_pressure_is_not_filtered_on_gsep(self):
        """Targets are not GSEP-filtered.

        An insertion is made into whatever elevated system is there; that
        system's own material has no bearing on whether it can receive one.
        Filtering these on GSEP too would discard most of the elevated network
        and silently shrink the candidate list.
        """
        frame = classify.classify(make_mains([
            (99, 12, None, 20, PSI, None),                          # not GSEP
            (config.ASSETTYPE_BARE_STEEL, 12, None, 20, PSI, None),  # GSEP
        ]), RESOLVED)
        selected = classify.other_pressure_targets(frame)
        assert len(selected) == 2
        assert set(selected[schema.GSEP_ELIGIBLE]) == {0, 1}

    def test_the_two_buckets_never_share_a_main(self):
        frame = classify.classify(make_mains([
            (config.ASSETTYPE_BARE_STEEL, 6, None, 30, WC, None),
            (config.ASSETTYPE_BARE_STEEL, 12, None, 20, PSI, None),
            (config.ASSETTYPE_BARE_STEEL, 12, None, 500, PSI, None),
        ]), RESOLVED)
        lower = classify.lower_pressure_candidates(frame)
        other = classify.other_pressure_targets(frame)
        assert set(lower["GLOBALID"]).isdisjoint(set(other["GLOBALID"]))
        # The 500 PSI main is in neither: above the Other Pressure ceiling.
        assert len(lower) + len(other) == 2
