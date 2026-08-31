"""Tests for the ArcGIS layer: field resolution, cache keys, geometry, deltas.

No network and no token. Everything here is either a pure function or works
against a fake layer-metadata dict of the shape the REST API returns.
"""
import json

import pytest

from pipelineinsertion import arcgis, config


def make_meta(field_names, object_id_field="OBJECTID", wkid=2249):
    return {
        "object_id_field": object_id_field,
        "wkid": wkid,
        "fields": [{"name": name} for name in field_names],
        "page_size": 2000,
    }


MAIN_LINES_FIELDS = [
    "OBJECTID", "GLOBALID", "legacyid", "ASSETGROUP", "ASSETTYPE",
    "nominaldiameter", "installationdate", "OPERATINGPRESSURE",
    "pressureunits", "MAOPRECORD", "LASTUPDATE",
]


class TestResolveFields:
    def test_resolves_every_purpose_on_a_full_layer(self):
        resolved = arcgis.resolve_fields(MAIN_LINES_FIELDS, "main_lines")
        assert resolved["assettype"] == "ASSETTYPE"
        assert resolved["diameter"] == "nominaldiameter"
        assert resolved["pressure"] == "OPERATINGPRESSURE"
        assert resolved["pressure_units"] == "pressureunits"
        assert resolved["maop"] == "MAOPRECORD"
        assert resolved["modified"] == "LASTUPDATE"

    def test_optional_fields_may_be_absent(self):
        # No legacyid and no MAOP: the run degrades rather than stopping.
        thin = ["OBJECTID", "GLOBALID", "ASSETTYPE", "OPERATINGPRESSURE",
                "pressureunits"]
        resolved = arcgis.resolve_fields(thin, "main_lines")
        assert resolved["legacyid"] is None
        assert resolved["maop"] is None

    @pytest.mark.parametrize("missing", ["ASSETTYPE", "OPERATINGPRESSURE",
                                         "pressureunits"])
    def test_a_missing_required_field_is_fatal_and_named(self, missing):
        # Without one of these no main can be classified at all, so the run
        # stops with a message naming what the layer does have.
        names = [name for name in MAIN_LINES_FIELDS if name != missing]
        with pytest.raises(RuntimeError) as caught:
            arcgis.resolve_fields(names, "main_lines")
        assert "main_lines" in str(caught.value)

    def test_alternative_spellings_resolve(self):
        names = ["OBJECTID", "assettype", "operatingpressure", "PRESSUREUNITS"]
        resolved = arcgis.resolve_fields(names, "main_lines")
        assert resolved["assettype"] == "assettype"
        assert resolved["pressure_units"] == "PRESSUREUNITS"


class TestBuildOutFields:
    def test_asks_only_for_what_it_reads(self):
        out_fields = arcgis.build_out_fields(make_meta(MAIN_LINES_FIELDS), "main_lines")
        assert out_fields != "*"
        requested = set(out_fields.split(","))
        assert "ASSETTYPE" in requested
        assert "pressureunits" in requested

    def test_never_repeats_a_field(self):
        out_fields = arcgis.build_out_fields(make_meta(MAIN_LINES_FIELDS), "main_lines")
        requested = out_fields.split(",")
        assert len(requested) == len(set(requested))

    def test_falls_back_to_everything_when_nothing_resolves(self):
        assert arcgis.build_out_fields(make_meta(["A", "B"]), "main_lines") == "*"


class TestOutFieldSignature:
    def test_stable_across_calls(self):
        assert arcgis.out_field_request_signature() == (
            arcgis.out_field_request_signature())

    def test_changes_when_the_requested_fields_change(self):
        """The check that stops a stale cache looking like a broken service.

        A delta refresh only re-downloads changed records, so a newly requested
        field would arrive for a handful of rows and be blank for the rest.
        """
        before = arcgis.out_field_request_signature()
        original = arcgis.OUT_FIELD_GROUPS
        try:
            arcgis.OUT_FIELD_GROUPS = original + (("SOMETHINGNEW",),)
            assert arcgis.out_field_request_signature() != before
        finally:
            arcgis.OUT_FIELD_GROUPS = original

    def test_insensitive_to_case_and_order_within_a_group(self):
        # A rename the resolver would treat as the same name must not
        # invalidate every cache in the field.
        before = arcgis.out_field_request_signature()
        original = arcgis.OUT_FIELD_GROUPS
        try:
            arcgis.OUT_FIELD_GROUPS = tuple(
                tuple(reversed([name.upper() for name in group]))
                for group in original)
            assert arcgis.out_field_request_signature() == before
        finally:
            arcgis.OUT_FIELD_GROUPS = original


class TestGeometry:
    def test_single_path_becomes_a_linestring(self):
        geometry = arcgis.esri_geometry_to_shape({"paths": [[[0, 0], [1, 1]]]})
        assert geometry.geom_type == "LineString"

    def test_multiple_paths_become_a_multilinestring(self):
        geometry = arcgis.esri_geometry_to_shape(
            {"paths": [[[0, 0], [1, 1]], [[5, 5], [6, 6]]]})
        assert geometry.geom_type == "MultiLineString"

    def test_extra_ordinates_are_ignored(self):
        # Esri paths carry z and m values the analysis does not use.
        geometry = arcgis.esri_geometry_to_shape(
            {"paths": [[[0, 0, 10, 1], [1, 1, 12, 2]]]})
        assert list(geometry.coords) == [(0.0, 0.0), (1.0, 1.0)]

    def test_a_one_point_path_is_not_a_line(self):
        assert arcgis.esri_geometry_to_shape({"paths": [[[0, 0]]]}) is None

    def test_point_geometry(self):
        assert arcgis.esri_geometry_to_shape({"x": 1, "y": 2}).geom_type == "Point"

    @pytest.mark.parametrize("value", [
        None, {}, {"paths": []}, {"x": None, "y": None}, {"type": "Nonsense"},
    ])
    def test_unusable_geometry_is_none_rather_than_raising(self, value):
        assert arcgis.esri_geometry_to_shape(value) is None


class TestCachePaths:
    def test_layer_name_becomes_a_safe_filename(self):
        assert arcgis.safe_cache_name("Main Lines (145)") == "main_lines_145"
        assert arcgis.safe_cache_name("main lines delta") == "main_lines_delta"

    def test_data_and_metadata_are_named_consistently(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "LAYER_CACHE_DIR", tmp_path)
        data_path, meta_path = arcgis.layer_cache_paths("main_lines")
        assert data_path.name == "main_lines.pkl.gz"
        assert meta_path.name == "main_lines.meta.json"

    def test_cache_folder_is_created(self, tmp_path, monkeypatch):
        target = tmp_path / "nested" / "cache"
        monkeypatch.setattr(config, "LAYER_CACHE_DIR", target)
        arcgis.layer_cache_paths("main_lines")
        assert target.is_dir()


class TestDeltaWatermark:
    def test_a_real_watermark_becomes_a_timestamp_literal(self):
        assert arcgis.epoch_ms_to_sql_timestamp(0) == (
            "timestamp '1970-01-01 00:00:00'")
        assert "1971-08-01" in arcgis.epoch_ms_to_sql_timestamp(49852800000)

    @pytest.mark.parametrize("value", [
        None, "", "abc", -1, float("nan"), float("inf"), 1e18,
    ])
    def test_an_unusable_watermark_widens_the_window_rather_than_narrowing_it(
            self, value):
        """Never wrong, only slower.

        A watermark that cannot be trusted must not become a window that skips
        records - so anything unusable falls back to the epoch, which makes the
        delta the whole history.
        """
        assert arcgis.epoch_ms_to_sql_timestamp(value) == (
            "timestamp '1970-01-01 00:00:00'")

    def test_delta_where_ands_onto_the_base_clause(self):
        clause = arcgis.build_delta_where("1=1", "LASTUPDATE", 0)
        assert clause.startswith("(1=1) AND LASTUPDATE > timestamp")


class TestUpsert:
    def test_changed_records_replace_their_cached_versions(self):
        import geopandas as gpd
        from shapely.geometry import Point

        cached = gpd.GeoDataFrame(
            {"OBJECTID": [1, 2], "v": ["old", "old"]},
            geometry=[Point(0, 0), Point(1, 1)], crs="EPSG:2249")
        delta = gpd.GeoDataFrame(
            {"OBJECTID": [2], "v": ["new"]},
            geometry=[Point(1, 1)], crs="EPSG:2249")

        merged = arcgis.upsert_cached_layer(cached, delta, "OBJECTID")
        assert len(merged) == 2
        assert dict(zip(merged["OBJECTID"], merged["v"])) == {1: "old", 2: "new"}

    def test_an_empty_delta_leaves_the_cache_alone(self):
        import geopandas as gpd
        from shapely.geometry import Point

        cached = gpd.GeoDataFrame(
            {"OBJECTID": [1]}, geometry=[Point(0, 0)], crs="EPSG:2249")
        assert arcgis.upsert_cached_layer(cached, None, "OBJECTID") is cached

    def test_a_missing_objectid_keeps_the_cache_rather_than_dropping_records(self):
        # Returning the delta alone would silently discard every unchanged
        # record, which looks like a service that lost most of its data.
        import geopandas as gpd
        from shapely.geometry import Point

        cached = gpd.GeoDataFrame(
            {"OBJECTID": [1, 2]}, geometry=[Point(0, 0), Point(1, 1)],
            crs="EPSG:2249")
        delta = gpd.GeoDataFrame(
            {"other": [1]}, geometry=[Point(0, 0)], crs="EPSG:2249")
        assert len(arcgis.upsert_cached_layer(cached, delta, "OBJECTID")) == 2


class TestChunkList:
    def test_splits_evenly(self):
        assert list(arcgis.chunk_list([1, 2, 3, 4], 2)) == [[1, 2], [3, 4]]

    def test_last_chunk_may_be_short(self):
        assert list(arcgis.chunk_list([1, 2, 3], 2)) == [[1, 2], [3]]

    def test_empty(self):
        assert list(arcgis.chunk_list([], 2)) == []


# The projection the National Grid services are actually published in. It has
# no EPSG code, which is the whole reason spatial_reference_of exists.
NG_WKT = (
    'PROJCS["NG_Equidistant_Conic_USft",GEOGCS["GCS_WGS_1984",'
    'DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],'
    'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],'
    'PROJECTION["Equidistant_Conic"],PARAMETER["False_Easting",0.0],'
    'PARAMETER["False_Northing",0.0],PARAMETER["Central_Meridian",-74.0],'
    'PARAMETER["Standard_Parallel_1",42.0],PARAMETER["Standard_Parallel_2",44.0],'
    'PARAMETER["Latitude_Of_Origin",43.0],UNIT["Foot_US",0.3048006096012192]]'
)


class TestSpatialReference:
    def test_a_wkid_is_read(self):
        layer_crs, wkid, _ = arcgis.spatial_reference_of(
            {"extent": {"spatialReference": {"wkid": 2249}}})
        assert layer_crs == "EPSG:2249"
        assert wkid == 2249

    def test_latest_wkid_is_preferred_over_wkid(self):
        layer_crs, wkid, _ = arcgis.spatial_reference_of(
            {"spatialReference": {"wkid": 102100, "latestWkid": 3857}})
        assert wkid == 3857
        assert layer_crs == "EPSG:3857"

    def test_a_custom_projection_is_read_from_its_wkt(self):
        """The defect that put every feature 600 miles out to sea.

        The National Grid services publish in a custom projection with no EPSG
        code, so its spatialReference is a bare {"wkt": ...}. Reading only
        wkid/latestWkid returned None, the frame was labelled with the fallback
        zone instead, and every position was wrong while the numbers stayed
        plausible - nothing raised.
        """
        layer_crs, wkid, raw = arcgis.spatial_reference_of(
            {"extent": {"spatialReference": {"wkt": NG_WKT}}})
        assert wkid is None
        assert layer_crs == NG_WKT
        assert raw["wkt"] == NG_WKT

    def test_the_custom_projection_is_usable_and_foot_based(self):
        from pipelineinsertion import crs as crs_module

        layer_crs, _, _ = arcgis.spatial_reference_of(
            {"spatialReference": {"wkt": NG_WKT}})
        # Foot-based, so the analysis keeps it and measures distances directly.
        assert crs_module.is_foot_based(layer_crs) is True
        assert crs_module.analysis_crs(layer_crs).name == "NG_Equidistant_Conic_USft"

    def test_wkt_wins_when_both_are_present(self):
        # The WKT is the more specific of the two.
        layer_crs, _, _ = arcgis.spatial_reference_of(
            {"spatialReference": {"wkid": 4326, "wkt": NG_WKT}})
        assert layer_crs == NG_WKT

    def test_the_layer_extent_is_preferred_over_the_service(self):
        layer_crs, _, _ = arcgis.spatial_reference_of({
            "extent": {"spatialReference": {"wkid": 2249}},
            "spatialReference": {"wkid": 4326},
        })
        assert layer_crs == "EPSG:2249"

    def test_nothing_at_all_is_none_rather_than_a_guess(self):
        layer_crs, wkid, raw = arcgis.spatial_reference_of({})
        assert layer_crs is None and wkid is None and raw == {}

    def test_a_custom_projection_is_not_reprojected_by_outsr(self):
        """No wkid means no outSR, so the service answers in its own reference.

        Sending an outSR the layer does not have would silently reproject the
        geometries away from the CRS the frame is then labelled with.
        """
        layer_crs, wkid, _ = arcgis.spatial_reference_of(
            {"spatialReference": {"wkt": NG_WKT}})
        assert wkid is None      # so `if meta.get("wkid")` skips outSR
        assert layer_crs is not None   # but the frame is still labelled


class TestCacheRejectsAnUnlabelledFrame:
    def test_a_cache_with_no_crs_is_refreshed(self, tmp_path, monkeypatch):
        """A cache written before the WKT was read carries no CRS.

        Nothing downstream can recover one, so it has to be re-downloaded -
        without the user needing to know why.
        """
        import geopandas as gpd
        from shapely.geometry import LineString

        monkeypatch.setattr(config, "LAYER_CACHE_DIR", tmp_path)
        monkeypatch.setattr(config, "USE_LAYER_CACHE", True)
        monkeypatch.setattr(config, "FORCE_LAYER_REFRESH", False)

        data_path, meta_path = arcgis.layer_cache_paths("main_lines")
        gpd.GeoDataFrame(
            {"OBJECTID": [1]}, geometry=[LineString([(0, 0), (1, 1)])], crs=None
        ).to_pickle(data_path, compression="gzip")
        meta_path.write_text(json.dumps({
            "layer_url": "u", "where_clause": "1=1",
            "out_field_signature": arcgis.out_field_request_signature(),
        }))

        cached, meta = arcgis.read_layer_cache("main_lines", "u", "1=1")
        assert cached is None, "an unlabelled cache must force a full refresh"

    def test_a_cache_with_a_crs_is_used(self, tmp_path, monkeypatch):
        import geopandas as gpd
        from shapely.geometry import LineString

        monkeypatch.setattr(config, "LAYER_CACHE_DIR", tmp_path)
        monkeypatch.setattr(config, "USE_LAYER_CACHE", True)
        monkeypatch.setattr(config, "FORCE_LAYER_REFRESH", False)

        data_path, meta_path = arcgis.layer_cache_paths("main_lines")
        gpd.GeoDataFrame(
            {"OBJECTID": [1]}, geometry=[LineString([(0, 0), (1, 1)])],
            crs="EPSG:2249").to_pickle(data_path, compression="gzip")
        meta_path.write_text(json.dumps({
            "layer_url": "u", "where_clause": "1=1",
            "out_field_signature": arcgis.out_field_request_signature(),
        }))

        cached, meta = arcgis.read_layer_cache("main_lines", "u", "1=1")
        assert cached is not None and len(cached) == 1
