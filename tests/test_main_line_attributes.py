"""Decoding ASSETTYPE and friends from the real layer 145 metadata.

Unlike the other tests, these run against the committed copy of the service's
own metadata under `reference/`, not a synthetic fixture. That is the point:
the decode is only correct if it matches what the service actually publishes,
and a fixture I wrote would agree with my assumptions rather than with the
layer.

The class that matters most is TestTheGsepCodesAreUnambiguous. Every ASSETTYPE
code on this layer belongs to a per-ASSETGROUP domain, so a code only means
something once the subtype is known. That the five GSEP codes happen to agree
across all eleven subtypes is what makes the flat production query correct -
it is a fact about this data, checked here, not an assumption.
"""
import json

import pytest
from shapely.geometry import LineString

from pipelineinsertion import classify, config, domains, schema, systems

REFERENCE = (config.REPO_ROOT / "reference" / "mapserver_json"
             / "MA_Pressure_View_MA" / "layer_145-Main_Lines.raw.json")

pytestmark = pytest.mark.skipif(
    not REFERENCE.is_file(),
    reason="the committed layer 145 metadata is not in this checkout")


@pytest.fixture(scope="module")
def layer_json():
    with open(REFERENCE, encoding="utf-8-sig") as handle:
        return json.load(handle)


class TestTheLayerIsWhatWeThink:
    def test_it_is_main_lines_145(self, layer_json):
        assert layer_json["id"] == 145
        assert layer_json["name"] == "Main Lines"

    def test_assetgroup_is_the_subtype_field(self, layer_json):
        # Which is why ASSETTYPE has to be decoded on the pair.
        assert layer_json["typeIdField"] == "ASSETGROUP"

    def test_the_spatial_reference_has_no_epsg_code(self, layer_json):
        """The layer is published in a custom projection.

        Its spatialReference carries a wkt and no wkid, which is what made
        every feature land 600 miles out to sea before it was read properly.
        """
        from pipelineinsertion import arcgis

        layer_crs, wkid, raw = arcgis.spatial_reference_of(layer_json)
        assert wkid is None
        assert "wkt" in raw
        assert layer_crs.startswith("PROJCS")

    def test_the_analysis_keeps_that_projection_because_it_is_in_feet(
            self, layer_json):
        from pipelineinsertion import arcgis, crs

        layer_crs, _, _ = arcgis.spatial_reference_of(layer_json)
        assert crs.is_foot_based(layer_crs) is True


class TestTheGsepCodesAreUnambiguous:
    """The fact the production query depends on."""

    EXPECTED = {
        config.ASSETTYPE_BARE_STEEL: "Bare Steel",
        config.ASSETTYPE_CAST_IRON: "Cast Iron",
        config.ASSETTYPE_COATED_STEEL: "Coated Steel",
        config.ASSETTYPE_COPPER: "Copper",
        config.ASSETTYPE_WROUGHT_IRON: "Wrought Iron",
    }

    @pytest.mark.parametrize("code,label", sorted(EXPECTED.items()))
    def test_each_gsep_code_means_the_same_in_every_subtype(
            self, layer_json, code, label):
        decoder = domains.subtype_decoder(layer_json, "ASSETTYPE")
        found = {name for (_, value), name in decoder.items() if value == code}
        assert found == {label}, (
            f"ASSETTYPE {code} is not {label!r} everywhere: {sorted(found)}. "
            f"The flat `ASSETTYPE = {code}` filter would be wrong.")

    def test_the_config_labels_match_the_service(self, layer_json):
        from pipelineinsertion import gsep

        decoder = domains.subtype_decoder(layer_json, "ASSETTYPE")
        for code, label in self.EXPECTED.items():
            assert gsep.MATERIAL_LABELS[code] == label
            assert domains.decode(decoder, 2, code) == label


class TestPressureUnitsDomain:
    def test_the_units_domain_matches_the_configured_codes(self, layer_json):
        """The single most consequential assumption in the workflow.

        If code 1 were water column rather than PSI, every main would be
        bucketed on the wrong unit and the run would still finish.
        """
        assert domains.check_pressure_units(layer_json) is True

    def test_the_codes_are_where_config_says(self, layer_json):
        labels = domains.labels_for(layer_json, "pressureunits")
        assert "pound" in labels[config.PRESSURE_UNIT_PSI].lower()
        assert "water" in labels[config.PRESSURE_UNIT_WC].lower()


class TestSubtypeDecoder:
    def test_it_covers_every_subtype(self, layer_json):
        decoder = domains.subtype_decoder(layer_json, "ASSETTYPE")
        groups = {group for group, _ in decoder}
        assert groups == set(domains.assetgroup_labels(layer_json))

    def test_assetgroups_are_named(self, layer_json):
        labels = domains.assetgroup_labels(layer_json)
        assert labels[2] == "Distribution Pipe"
        assert labels[1] == "Service Pipe"

    def test_decode_uses_the_pair(self, layer_json):
        decoder = domains.subtype_decoder(layer_json, "ASSETTYPE")
        assert domains.decode(decoder, 2, 2) == "Cast Iron"
        assert domains.decode(decoder, 1, 5) == "Copper"
        assert domains.decode(decoder, 2, 9) == "Plastic PE"

    def test_a_missing_subtype_falls_back_to_an_unambiguous_code(self, layer_json):
        # A row with no ASSETGROUP still gets named where the code can only
        # mean one thing.
        decoder = domains.subtype_decoder(layer_json, "ASSETTYPE")
        assert domains.decode(decoder, None, 2) == "Cast Iron"

    def test_an_ambiguous_code_reports_as_its_code(self, layer_json):
        """999 is 'UNK' under one subtype and 'Unknown Type' under another.

        A label that might belong to a different subtype is worse than no
        label, so the code is returned instead.
        """
        decoder = domains.subtype_decoder(layer_json, "ASSETTYPE")
        assert domains.decode(decoder, None, 999) == "999"

    def test_a_missing_code_is_blank(self, layer_json):
        decoder = domains.subtype_decoder(layer_json, "ASSETTYPE")
        assert domains.decode(decoder, 2, None) == ""

    def test_no_metadata_gives_an_empty_decoder(self):
        assert domains.subtype_decoder(None, "ASSETTYPE") == {}
        assert domains.decode({}, 2, 2) == "2"


class TestPlasticIsReportedNotAssumed:
    def test_the_plastics_the_service_publishes_are_listed(self, layer_json):
        # For the decision that config.PLASTIC_ASSETTYPES records. Listing them
        # is not the same as enabling them.
        plastics = domains.plastic_assettypes(layer_json)
        assert set(plastics) >= {7, 8, 9, 10, 13}
        assert plastics[9] == "Plastic PE"

    def test_none_of_them_are_eligible_until_configured(self, layer_json):
        from pipelineinsertion import gsep

        assert config.PLASTIC_ASSETTYPES == ()
        for code in domains.plastic_assettypes(layer_json):
            assert gsep.is_eligible(code, 6) is False


# --- The attributes on the output layers -------------------------------------

RESOLVED = {
    "globalid": "GLOBALID", "legacyid": "legacyid", "assetgroup": "ASSETGROUP",
    "assettype": "ASSETTYPE", "diameter": "nominaldiameter",
    "installed": "installationdate", "pressure": "OPERATINGPRESSURE",
    "pressure_units": "pressureunits", "maop": "MAOPRECORD",
    "cpsubnetwork": "cpsubnetworkname",
}

WC = config.PRESSURE_UNIT_WC
INSTALLED_1960 = -315619200000
INSTALLED_1990 = 631152000000


def make_mains(rows):
    """Mains spelled the way layer 145 spells them.

    The lowercase source names matter: `nominaldiameter` against the canonical
    `NOMINALDIAMETER` is the case collision a GeoPackage cannot hold.
    """
    import geopandas as gpd

    records, geometries = [], []
    for index, (group, assettype, diameter, installed, cp, offset) in enumerate(rows):
        records.append({
            "GLOBALID": "{G%d}" % index, "legacyid": index,
            "ASSETGROUP": group, "ASSETTYPE": assettype,
            "nominaldiameter": diameter, "installationdate": installed,
            "OPERATINGPRESSURE": 30, "pressureunits": WC, "MAOPRECORD": None,
            "cpsubnetworkname": cp,
        })
        geometries.append(LineString([(offset, 0), (offset + 100, 0)]))
    return gpd.GeoDataFrame(records, geometry=geometries, crs="EPSG:2249")


class TestMainsCarryDecodedAttributes:
    @pytest.fixture
    def classified(self, layer_json):
        return classify.classify(
            make_mains([
                (2, config.ASSETTYPE_CAST_IRON, 8, INSTALLED_1960, "CP-A", 0),
                (1, config.ASSETTYPE_COPPER, 4, INSTALLED_1960, "CP-B", 100),
                (2, 9, 6, INSTALLED_1990, "CP-A", 200),
            ]),
            RESOLVED, layer_json=layer_json)

    def test_every_declared_attribute_column_is_present(self, classified):
        for name in schema.MAIN_ATTRIBUTE_FIELDS:
            assert name in classified.columns

    def test_assettype_is_decoded_on_the_pair(self, classified):
        assert list(classified[schema.ASSETTYPE_DECODED]) == [
            "Cast Iron", "Copper", "Plastic PE"]

    def test_assetgroup_is_decoded(self, classified):
        assert list(classified[schema.ASSETGROUP_DECODED]) == [
            "Distribution Pipe", "Service Pipe", "Distribution Pipe"]

    def test_the_raw_codes_are_kept_beside_the_labels(self, classified):
        # The code is what the production query filters on and what a record
        # traces back to; a label can be edited on the service.
        assert list(classified[schema.ASSETTYPE]) == [2, 5, 9]
        assert list(classified[schema.ASSETGROUP]) == [2, 1, 2]

    def test_diameter_and_cp_subnetwork_are_carried(self, classified):
        assert list(classified[schema.NOMINAL_DIAMETER]) == [8, 4, 6]
        assert list(classified[schema.CP_SUBNETWORK]) == ["CP-A", "CP-B", "CP-A"]

    def test_the_installation_date_is_readable_as_well_as_raw(self, classified):
        # Epoch milliseconds are unreadable in a desktop GIS.
        assert list(classified[schema.INSTALLATION_DATE_ISO]) == [
            "1960-01-01", "1960-01-01", "1990-01-01"]
        assert list(classified[schema.INSTALLATION_DATE]) == [
            INSTALLED_1960, INSTALLED_1960, INSTALLED_1990]

    def test_no_two_columns_differ_only_in_case(self, classified):
        """A GeoPackage cannot hold `nominaldiameter` and `NOMINALDIAMETER`.

        GDAL rejects the second with "Error adding field" and says nothing
        about why, so the source spelling is replaced rather than kept.
        """
        lowered = [str(name).lower() for name in classified.columns]
        duplicated = sorted({name for name in lowered if lowered.count(name) > 1})
        assert duplicated == []

    def test_the_layer_spelling_is_replaced_not_duplicated(self, classified):
        assert "nominaldiameter" not in classified.columns
        assert schema.NOMINAL_DIAMETER in classified.columns

    def test_it_still_works_with_no_metadata_at_all(self):
        # Falls back to the labels this project names itself.
        frame = classify.classify(
            make_mains([(2, config.ASSETTYPE_CAST_IRON, 8, INSTALLED_1960, "CP-A", 0)]),
            RESOLVED, domain_labels={}, layer_json={})
        assert frame.iloc[0][schema.ASSETTYPE_DECODED] == "Cast Iron"

    def test_a_layer_without_cpsubnetworkname_leaves_it_blank(self, layer_json):
        frame = make_mains([
            (2, config.ASSETTYPE_CAST_IRON, 8, INSTALLED_1960, "CP-A", 0)
        ]).drop(columns=["cpsubnetworkname"])
        classified = classify.classify(
            frame, dict(RESOLVED, cpsubnetwork=None), layer_json=layer_json)
        assert list(classified[schema.CP_SUBNETWORK]) == [""]


class TestSystemsSummariseTheirMains:
    @pytest.fixture
    def dissolved(self, layer_json):
        # Three contiguous mains: two cast iron in CP-A, one copper in CP-B.
        classified = classify.classify(
            make_mains([
                (2, config.ASSETTYPE_CAST_IRON, 8, INSTALLED_1960, "CP-A", 0),
                (2, config.ASSETTYPE_CAST_IRON, 12, INSTALLED_1990, "CP-A", 100),
                (1, config.ASSETTYPE_COPPER, 4, INSTALLED_1960, "CP-B", 200),
            ]),
            RESOLVED, layer_json=layer_json)
        lower = classify.lower_pressure_candidates(classified)
        return systems.dissolve(lower, "GLOBALID", "legacyid")

    def test_one_contiguous_system(self, dissolved):
        assert len(dissolved) == 1
        assert dissolved.iloc[0][schema.MAIN_COUNT] == 3

    def test_every_declared_system_attribute_is_present(self, dissolved):
        for name in schema.SYSTEM_ATTRIBUTE_FIELDS:
            assert name in dissolved.columns

    def test_materials_are_the_distinct_set(self, dissolved):
        # A single ASSETTYPE on a dissolved system would be a lie.
        assert dissolved.iloc[0][schema.MATERIALS] == "Cast Iron;Copper"

    def test_assetgroups_are_the_distinct_set(self, dissolved):
        assert dissolved.iloc[0][schema.ASSETGROUPS] == (
            "Distribution Pipe;Service Pipe")

    def test_the_diameter_range_spans_the_mains(self, dissolved):
        assert dissolved.iloc[0][schema.MIN_DIAMETER] == 4
        assert dissolved.iloc[0][schema.MAX_DIAMETER] == 12

    def test_the_install_dates_span_the_mains(self, dissolved):
        assert dissolved.iloc[0][schema.EARLIEST_INSTALL] == "1960-01-01"
        assert dissolved.iloc[0][schema.LATEST_INSTALL] == "1990-01-01"

    def test_a_system_crossing_two_cp_subnetworks_says_so(self, dissolved):
        """A real constructability finding, invisible without this.

        The count is separate from the list because nobody filters on a
        semicolon-separated string.
        """
        assert dissolved.iloc[0][schema.CP_SUBNETWORKS] == "CP-A;CP-B"
        assert dissolved.iloc[0][schema.CP_SUBNETWORK_COUNT] == 2

    def test_an_empty_dissolve_still_declares_the_columns(self, layer_json):
        classified = classify.classify(
            make_mains([(2, config.ASSETTYPE_CAST_IRON, 8, INSTALLED_1960, "CP-A", 0)]),
            RESOLVED, layer_json=layer_json)
        empty = systems.dissolve(classified.iloc[0:0], "GLOBALID", "legacyid")
        for name in schema.SYSTEM_ATTRIBUTE_FIELDS:
            assert name in empty.columns


class TestTheAttributesSurviveToTheGeoPackage:
    def test_a_written_layer_reads_back_with_its_attributes(
            self, layer_json, tmp_path, monkeypatch):
        import geopandas as gpd

        monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(config, "OUTPUT_GPKG", tmp_path / "out.gpkg")

        import pipeline_insertion_evaluator as workflow

        classified = classify.classify(
            make_mains([
                (2, config.ASSETTYPE_CAST_IRON, 8, INSTALLED_1960, "CP-A", 0),
                (1, config.ASSETTYPE_COPPER, 4, INSTALLED_1960, "CP-B", 100),
            ]),
            RESOLVED, layer_json=layer_json)
        lower = classify.lower_pressure_candidates(classified)
        dissolved = systems.dissolve(lower, "GLOBALID", "legacyid")

        workflow.write_outputs({
            schema.GSEP_LOWER_PRESSURE_LAYER: lower,
            schema.LOWER_PRESSURE_SYSTEMS_LAYER: dissolved,
        })

        mains_back = gpd.read_file(tmp_path / "out.gpkg",
                                   layer=schema.GSEP_LOWER_PRESSURE_LAYER)
        for name in schema.MAIN_ATTRIBUTE_FIELDS:
            assert name in mains_back.columns, f"{name} was lost on write"
        assert set(mains_back[schema.ASSETTYPE_DECODED]) == {"Cast Iron", "Copper"}
        assert set(mains_back[schema.CP_SUBNETWORK]) == {"CP-A", "CP-B"}

        systems_back = gpd.read_file(tmp_path / "out.gpkg",
                                     layer=schema.LOWER_PRESSURE_SYSTEMS_LAYER)
        for name in schema.SYSTEM_ATTRIBUTE_FIELDS:
            assert name in systems_back.columns, f"{name} was lost on write"

    def test_a_case_collision_is_reported_rather_than_left_to_gdal(self, tmp_path):
        """GDAL says "Error adding field" and nothing about the collision."""
        import geopandas as gpd

        import pipeline_insertion_evaluator as workflow

        frame = gpd.GeoDataFrame(
            {"NOMINALDIAMETER": [8], "nominaldiameter": [8]},
            geometry=[LineString([(0, 0), (1, 1)])], crs="EPSG:2249")
        with pytest.raises(RuntimeError, match="differ only in case"):
            workflow.check_column_names(frame, "a layer")
