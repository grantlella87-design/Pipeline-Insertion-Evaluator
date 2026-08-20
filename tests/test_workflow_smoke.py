"""End-to-end: classify -> dissolve -> near -> select -> GeoPackage -> map.

The unit tests each pin one rule. This runs the whole analysis half on a small
synthetic network with a known right answer, writes the GeoPackage, and loads
it back through the map server - which is the only thing that catches a stage
whose output the next stage cannot read.

Still no network and no token: the download stage is the one part not exercised
here, because it is the one part that needs a service.
"""
import pytest
from shapely.geometry import LineString

from pipelineinsertion import classify, config, nearest, schema, systems

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

RESOLVED = {
    "globalid": "GLOBALID", "legacyid": "legacyid", "assettype": "ASSETTYPE",
    "diameter": "nominaldiameter", "installed": "installationdate",
    "pressure": "OPERATINGPRESSURE", "pressure_units": "pressureunits",
    "maop": "MAOPRECORD",
}

WC = config.PRESSURE_UNIT_WC
PSI = config.PRESSURE_UNIT_PSI

# Placed inside the Massachusetts Mainland State Plane zone, so the
# reprojection the map does lands in Massachusetts rather than the Atlantic.
ORIGIN_X, ORIGIN_Y = 760000, 2960000


def at(x0, y0, x1, y1):
    return LineString([(ORIGIN_X + x0, ORIGIN_Y + y0),
                       (ORIGIN_X + x1, ORIGIN_Y + y1)])


# (label, assettype, diameter, installed, pressure, units, geometry)
NETWORK = [
    # Two contiguous cast iron mains at 30 WC, 30 ft from an elevated system.
    # -> one system, one candidate.
    ("A1", config.ASSETTYPE_CAST_IRON, 8, None, 30, WC, at(0, 0, 50, 0)),
    ("A2", config.ASSETTYPE_CAST_IRON, 8, None, 30, WC, at(50, 0, 100, 0)),
    # A bare steel main at the same pressure, but 800 ft from anything.
    # -> its own system, excluded as too far.
    ("B1", config.ASSETTYPE_BARE_STEEL, 6, None, 30, WC, at(0, 800, 100, 800)),
    # Cast iron at 16 inches: not GSEP eligible, so not a candidate at all.
    ("C1", config.ASSETTYPE_CAST_IRON, 16, None, 30, WC, at(0, 20, 100, 20)),
    # The insertion target: 20 PSI, 30 ft from A1/A2.
    ("T1", config.ASSETTYPE_COATED_STEEL, 12, None, 20, PSI, at(0, 30, 100, 30)),
]


@pytest.fixture
def analysed():
    import geopandas as gpd

    frame = gpd.GeoDataFrame(
        [{"GLOBALID": "{%s}" % label, "legacyid": label.lower(),
          "ASSETTYPE": assettype, "nominaldiameter": diameter,
          "installationdate": installed, "OPERATINGPRESSURE": value,
          "pressureunits": units, "MAOPRECORD": None}
         for label, assettype, diameter, installed, value, units, _ in NETWORK],
        geometry=[row[6] for row in NETWORK], crs="EPSG:2249")

    classified = classify.classify(frame, RESOLVED)
    lower_mains = classify.lower_pressure_candidates(classified)
    other_mains = classify.other_pressure_targets(classified)
    lower_systems = systems.dissolve(lower_mains, "GLOBALID", "legacyid")
    other_systems = systems.dissolve(other_mains, "GLOBALID", "legacyid")
    near, paths, candidates = nearest.analyse(lower_systems, other_systems)
    return {
        "lower_mains": lower_mains, "other_mains": other_mains,
        "lower_systems": lower_systems, "other_systems": other_systems,
        "near": near, "paths": paths, "candidates": candidates,
    }


class TestTheKnownAnswer:
    def test_only_gsep_eligible_mains_reach_the_lower_bucket(self, analysed):
        # C1 is cast iron at 16 inches, over the 14 inch limit.
        assert sorted(analysed["lower_mains"]["GLOBALID"]) == ["{A1}", "{A2}", "{B1}"]

    def test_the_target_is_not_gsep_filtered(self, analysed):
        assert list(analysed["other_mains"]["GLOBALID"]) == ["{T1}"]

    def test_contiguous_mains_dissolve_and_a_detached_one_does_not(self, analysed):
        systems_frame = analysed["lower_systems"]
        assert len(systems_frame) == 2
        counts = sorted(systems_frame[schema.MAIN_COUNT])
        assert counts == [1, 2]

    def test_exactly_one_candidate(self, analysed):
        assert len(analysed["candidates"]) == 1

    def test_the_candidate_traces_back_to_both_source_mains(self, analysed):
        assert analysed["candidates"].iloc[0][schema.SOURCE_IDS] == "{A1}|a1;{A2}|a2"

    def test_the_candidate_records_its_distance_and_target(self, analysed):
        candidate = analysed["candidates"].iloc[0]
        assert candidate[schema.DISTANCE_FT] == pytest.approx(30.0)
        assert candidate[schema.NEAREST_EP_PRESSURE] == 20.0

    def test_every_lower_system_is_accounted_for(self, analysed):
        statuses = sorted(analysed["near"][schema.CANDIDATE_STATUS])
        assert statuses == [nearest.STATUS_CANDIDATE, nearest.STATUS_TOO_FAR]

    def test_a_water_column_candidate_passed_against_a_psi_target(self, analysed):
        # 30" WC is about 1.08 PSI against a 20 PSI target. Compared as raw
        # numbers - 20 >= 30 - this candidate would have been dropped.
        candidate = analysed["candidates"].iloc[0]
        assert candidate[schema.SYSTEM_PRESSURE] == 30.0
        assert candidate[schema.SYSTEM_PRESSURE_UNITS] == WC
        assert candidate[schema.SYSTEM_PRESSURE_PSI] < candidate[
            schema.NEAREST_EP_PRESSURE_PSI]


class TestGeoPackage:
    @pytest.fixture
    def written(self, analysed, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(config, "OUTPUT_GPKG", tmp_path / "out.gpkg")

        import pipeline_insertion_evaluator as workflow

        counts = workflow.write_outputs({
            schema.GSEP_LOWER_PRESSURE_LAYER: analysed["lower_mains"],
            schema.OTHER_PRESSURE_MAINS_LAYER: analysed["other_mains"],
            schema.LOWER_PRESSURE_SYSTEMS_LAYER: analysed["lower_systems"],
            schema.ELEVATED_PRESSURE_SYSTEMS_LAYER: analysed["other_systems"],
            schema.INSERTION_PATHS_LAYER: analysed["paths"],
            schema.NEAR_AUDIT_TABLE: analysed["near"],
            schema.CANDIDATES_LAYER: analysed["candidates"],
        })
        return tmp_path / "out.gpkg", counts

    def test_every_layer_in_the_readme_inventory_is_written(self, written):
        _, counts = written
        for name in (schema.GSEP_LOWER_PRESSURE_LAYER,
                     schema.OTHER_PRESSURE_MAINS_LAYER,
                     schema.LOWER_PRESSURE_SYSTEMS_LAYER,
                     schema.ELEVATED_PRESSURE_SYSTEMS_LAYER,
                     schema.INSERTION_PATHS_LAYER,
                     schema.CANDIDATES_LAYER):
            assert name in counts

    def test_the_candidate_layer_reads_back(self, written):
        import geopandas as gpd

        path, _ = written
        candidates = gpd.read_file(path, layer=schema.CANDIDATES_LAYER)
        assert len(candidates) == 1
        assert candidates.iloc[0][schema.SOURCE_IDS] == "{A1}|a1;{A2}|a2"

    def test_geometries_are_written_as_one_type(self, written):
        # A GeoPackage layer holds one geometry type, and a dissolve produces a
        # mix of LineString and MultiLineString.
        import geopandas as gpd

        path, _ = written
        systems_frame = gpd.read_file(path, layer=schema.LOWER_PRESSURE_SYSTEMS_LAYER)
        assert set(systems_frame.geom_type) == {"MultiLineString"}

    def test_a_rerun_replaces_rather_than_appends(self, analysed, written):
        """A previous run's candidates must not survive into this one.

        Appending would leave a stale result to be reviewed as a current one.
        """
        import geopandas as gpd

        import pipeline_insertion_evaluator as workflow

        path, _ = written
        workflow.write_outputs({schema.CANDIDATES_LAYER: analysed["candidates"]})
        assert len(gpd.read_file(path, layer=schema.CANDIDATES_LAYER)) == 1


class TestMapServerReadsWhatTheWorkflowWrote:
    def test_layers_load_and_the_page_renders(self, analysed, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(config, "OUTPUT_GPKG", tmp_path / "out.gpkg")

        import pipeline_insertion_evaluator as workflow

        workflow.write_outputs({
            schema.LOWER_PRESSURE_SYSTEMS_LAYER: analysed["lower_systems"],
            schema.ELEVATED_PRESSURE_SYSTEMS_LAYER: analysed["other_systems"],
            schema.INSERTION_PATHS_LAYER: analysed["paths"],
            schema.CANDIDATES_LAYER: analysed["candidates"],
        })

        import importlib

        import leaflet_bbox_server as server

        # The module reads OUTPUT_GPKG at import time, so it is reloaded after
        # the patch rather than being handed a path it will not look at.
        importlib.reload(server)
        server.DATA.clear()
        server.LAYER_NOTES.clear()
        server.load_all()

        assert len(server.DATA["candidates"]) == 1
        assert server.BOUNDS["center_lat"] == pytest.approx(42.4, abs=0.5)

        page = server.html_page()
        assert "LPP GSEP" in page
        assert schema.SOURCE_IDS in page

    def test_a_missing_geopackage_gives_an_empty_map_not_a_traceback(
            self, tmp_path, monkeypatch):
        # A fresh checkout has no GeoPackage, and a traceback instead of a map
        # is not a useful answer to that.
        monkeypatch.setattr(config, "OUTPUT_GPKG", tmp_path / "absent.gpkg")

        import importlib

        import leaflet_bbox_server as server

        importlib.reload(server)
        server.DATA.clear()
        server.LAYER_NOTES.clear()
        server.load_all()

        assert len(server.DATA["candidates"]) == 0
        assert "candidates" in server.LAYER_NOTES
        assert server.BOUNDS == server.FALLBACK_BOUNDS
        assert "not available" in server.html_page()
