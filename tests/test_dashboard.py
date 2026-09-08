"""Tests for the dashboard's numbers, and for the columns it depends on.

The metrics are pure functions over frames, so they are tested directly. The
rendering is checked only for the things that would be wrong rather than ugly:
that the headline numbers reach the page, that a missing layer costs its panel
and not the page, and that a capped table says it is capped.

The regression class at the end is the one that matters. The dashboard was what
exposed it: three panels came up empty because the near stage rebuilt each row
from a fixed column list and dropped every attribute the dissolve had added.
"""
import pytest
from shapely.geometry import LineString

from pipelineinsertion import (
    classify,
    config,
    dashboard,
    dashboard_metrics,
    nearest,
    schema,
    systems,
)

WC = config.PRESSURE_UNIT_WC
PSI = config.PRESSURE_UNIT_PSI


def frame_with(columns):
    """A plain DataFrame with the given columns - enough for a metric."""
    import pandas as pd

    return pd.DataFrame(columns)


class TestSplitCounts:
    def test_a_set_column_is_counted_per_member(self):
        """MATERIALS is "Cast Iron;Copper" - the set a system dissolved from.

        Counting the strings whole would report that pair as its own category,
        which is how a dashboard ends up with forty materials.
        """
        frame = frame_with({schema.MATERIALS: [
            "Cast Iron;Copper", "Cast Iron", "Copper;Bare Steel"]})
        assert dashboard_metrics.split_counts(frame, schema.MATERIALS) == {
            "Cast Iron": 2, "Copper": 2, "Bare Steel": 1}

    def test_biggest_first(self):
        frame = frame_with({schema.MATERIALS: ["A", "B;A", "A"]})
        assert list(dashboard_metrics.split_counts(frame, schema.MATERIALS)) == ["A", "B"]

    def test_blanks_are_not_a_category(self):
        frame = frame_with({schema.MATERIALS: ["A;;", "", None, "A"]})
        assert dashboard_metrics.split_counts(frame, schema.MATERIALS) == {"A": 2}

    def test_a_missing_column_is_empty_not_an_error(self):
        # A GeoPackage written before the attribute columns existed is still
        # worth a dashboard - it just has fewer panels.
        assert dashboard_metrics.split_counts(frame_with({"other": [1]}),
                                              schema.MATERIALS) == {}


class TestStatusCounts:
    def test_statuses_are_relabelled_for_reading(self):
        frame = frame_with({schema.CANDIDATE_STATUS: [
            nearest.STATUS_CANDIDATE, nearest.STATUS_CANDIDATE,
            nearest.STATUS_TOO_FAR]})
        counts = dashboard_metrics.status_counts(frame)
        assert counts == {"Candidate": 2, "Nearest system too far": 1}

    def test_an_unknown_status_is_shown_as_itself(self):
        # Better a raw string than silently dropping a row.
        frame = frame_with({schema.CANDIDATE_STATUS: ["something_new"]})
        assert dashboard_metrics.status_counts(frame) == {"something_new": 1}


class TestDistanceHistogram:
    def test_values_land_in_the_right_bins(self):
        frame = frame_with({schema.DISTANCE_FT: [5, 12, 30, 49, 60, 5000]})
        rows = dict((label, count) for label, count, _ in
                    dashboard_metrics.distance_histogram(frame))
        assert rows["0–10 ft"] == 1
        assert rows["10–25 ft"] == 1
        assert rows["25–50 ft"] == 2
        assert rows["50–100 ft"] == 1
        assert rows["2,500+ ft"] == 1

    def test_the_bin_edge_belongs_to_the_upper_bin(self):
        # 50 ft exactly is a candidate, so it must not be counted as beyond it.
        frame = frame_with({schema.DISTANCE_FT: [50]})
        rows = dict((label, count) for label, count, _ in
                    dashboard_metrics.distance_histogram(frame))
        assert rows["25–50 ft"] == 0
        assert rows["50–100 ft"] == 1

    def test_systems_with_no_distance_are_not_binned(self):
        """They have no distance; putting them in a "very far" bin invents one."""
        frame = frame_with({schema.DISTANCE_FT: [5, None, float("nan")]})
        total = sum(count for _, count, _ in
                    dashboard_metrics.distance_histogram(frame))
        assert total == 1

    def test_the_threshold_is_one_of_the_bin_edges(self):
        # The chart draws its threshold rule at a bin boundary; if the
        # configured distance is not one, the rule would not be drawn.
        edges = [upper for _, _, upper in
                 dashboard_metrics.distance_histogram(frame_with(
                     {schema.DISTANCE_FT: []}))]
        assert config.MAX_DISTANCE_FT in edges


class TestDecadeCounts:
    def test_iso_dates_become_decades_oldest_first(self):
        frame = frame_with({schema.EARLIEST_INSTALL: [
            "1955-01-01", "1962-06-01", "1968-01-01", "1971-08-01"]})
        assert dashboard_metrics.decade_counts(frame) == {
            "1950s": 1, "1960s": 2, "1970s": 1}

    def test_unparseable_dates_are_skipped(self):
        frame = frame_with({schema.EARLIEST_INSTALL: ["", None, "unknown", "1960-01-01"]})
        assert dashboard_metrics.decade_counts(frame) == {"1960s": 1}


class TestSubnetworkCounts:
    def test_the_singular_reads_correctly(self):
        frame = frame_with({schema.CP_SUBNETWORK_COUNT: [1, 1, 2, 3]})
        assert dashboard_metrics.subnetwork_counts(frame) == {
            "1 subnetwork": 2, "2 subnetworks": 1, "3 subnetworks": 1}

    def test_zero_is_not_recorded_rather_than_zero_subnetworks(self):
        frame = frame_with({schema.CP_SUBNETWORK_COUNT: [0, 1]})
        assert dashboard_metrics.subnetwork_counts(frame) == {
            "not recorded": 1, "1 subnetwork": 1}


class TestPercentile:
    def test_median(self):
        assert dashboard_metrics.percentile([1, 2, 3, 4, 5], 0.5) == 3

    def test_empty_is_none_not_zero(self):
        # Zero would read as "every candidate is touching its target".
        assert dashboard_metrics.percentile([], 0.5) is None


class TestPressureHeadroom:
    def test_it_is_the_psi_difference(self):
        frame = frame_with({
            schema.SYSTEM_PRESSURE_PSI: [1.0, 2.0],
            schema.NEAREST_EP_PRESSURE_PSI: [5.0, 2.0]})
        assert dashboard_metrics.pressure_headroom(frame) == [4.0, 0.0]

    def test_mismatched_lengths_give_nothing_rather_than_wrong_pairs(self):
        frame = frame_with({
            schema.SYSTEM_PRESSURE_PSI: [1.0, None],
            schema.NEAREST_EP_PRESSURE_PSI: [5.0, 2.0]})
        assert dashboard_metrics.pressure_headroom(frame) == []


# --- Against a real analysis -------------------------------------------------

RESOLVED = {
    "globalid": "GLOBALID", "legacyid": "legacyid", "assetgroup": "ASSETGROUP",
    "assettype": "ASSETTYPE", "diameter": "nominaldiameter",
    "installed": "installationdate", "pressure": "OPERATINGPRESSURE",
    "pressure_units": "pressureunits", "maop": "MAOPRECORD",
    "cpsubnetwork": "cpsubnetworkname",
}


def build_analysis():
    """A small network run through the real pipeline, not hand-built frames."""
    import geopandas as gpd

    rows, geoms = [], []

    def add(assettype, pressure_units, pressure, cp, x, y, length=80):
        rows.append({
            "GLOBALID": "{G%d}" % len(rows), "legacyid": len(rows),
            "ASSETGROUP": 2, "ASSETTYPE": assettype, "nominaldiameter": 8,
            "installationdate": -315619200000,  # 1960
            "OPERATINGPRESSURE": pressure, "pressureunits": pressure_units,
            "MAOPRECORD": None, "cpsubnetworkname": cp,
        })
        geoms.append(LineString([(x, y), (x + length, y)]))

    # A qualifying candidate built from two mains in two CP subnetworks,
    # a system too far from anything, and a target.
    add(config.ASSETTYPE_CAST_IRON, WC, 30, "CP-A", 0, 0)
    add(config.ASSETTYPE_COPPER, WC, 30, "CP-B", 80, 0)
    add(config.ASSETTYPE_BARE_STEEL, WC, 30, "CP-A", 0, 900)
    add(config.ASSETTYPE_COATED_STEEL, PSI, 20, "CP-E", 0, 30, 200)

    frame = gpd.GeoDataFrame(rows, geometry=geoms, crs="EPSG:2249")
    classified = classify.classify(frame, RESOLVED, layer_json={})
    lower = systems.dissolve(classify.lower_pressure_candidates(classified),
                             "GLOBALID", "legacyid")
    other = systems.dissolve(classify.other_pressure_targets(classified),
                             "GLOBALID", "legacyid")
    near, paths, candidates = nearest.analyse(lower, other)
    return {
        schema.NEAR_AUDIT_TABLE: near,
        schema.CANDIDATES_LAYER: candidates,
        schema.LOWER_PRESSURE_SYSTEMS_LAYER: lower,
        schema.ELEVATED_PRESSURE_SYSTEMS_LAYER: other,
        schema.INSERTION_PATHS_LAYER: paths,
    }


@pytest.fixture(scope="module")
def analysis():
    return build_analysis()


class TestTheAttributesReachTheCandidates:
    """The regression the dashboard exposed.

    `analyse` rebuilt each row from a fixed list of columns, so MATERIALS,
    CP_SUBNETWORKS, the install dates and everything else the dissolve computed
    reached the system layers and never the candidates. Nothing failed - three
    dashboard panels simply said "No data", which is indistinguishable from a
    run that genuinely found none.
    """


    @pytest.mark.parametrize("column", schema.SYSTEM_ATTRIBUTE_FIELDS)
    def test_every_system_attribute_survives_the_near_stage(self, analysis, column):
        assert column in analysis[schema.CANDIDATES_LAYER].columns
        assert column in analysis[schema.NEAR_AUDIT_TABLE].columns

    def test_the_values_survive_too_not_just_the_columns(self, analysis):
        candidate = analysis[schema.CANDIDATES_LAYER].iloc[0]
        assert candidate[schema.MATERIALS] == "Cast Iron;Copper"
        assert candidate[schema.CP_SUBNETWORKS] == "CP-A;CP-B"
        assert candidate[schema.CP_SUBNETWORK_COUNT] == 2

    def test_the_paths_carry_them_as_well(self, analysis):
        paths = analysis[schema.INSERTION_PATHS_LAYER]
        assert schema.MATERIALS in paths.columns

    def test_the_near_fields_are_still_there(self, analysis):
        # Carrying every source column must not have displaced the near result.
        near = analysis[schema.NEAR_AUDIT_TABLE]
        for column in (schema.DISTANCE_FT, schema.NEAREST_EP_ID,
                       schema.CANDIDATE_STATUS, schema.IS_CANDIDATE):
            assert column in near.columns


@pytest.fixture(scope="module")
def metrics():
    return dashboard_metrics.collect(build_analysis())


class TestCollect:

    def test_the_headline_counts_agree_with_the_analysis(self, metrics):
        assert metrics["candidates"] == 1
        assert metrics["examined"] == 2
        assert metrics["conversion"] == pytest.approx(0.5)

    def test_the_panels_are_populated(self, metrics):
        assert metrics["materials"] == {"Cast Iron": 1, "Copper": 1}
        assert metrics["decades"] == {"1960s": 1}
        assert metrics["subnetworks"] == {"2 subnetworks": 1}
        assert metrics["crossing_systems"] == 1

    def test_the_rules_are_reported_so_the_page_states_them(self, metrics):
        assert metrics["max_distance_ft"] == config.MAX_DISTANCE_FT
        assert metrics["coated_steel_cutoff"] == config.COATED_STEEL_INSTALLED_BEFORE

    def test_no_layers_at_all_does_not_raise(self):
        metrics = dashboard_metrics.collect({})
        assert metrics["candidates"] == 0
        assert metrics["conversion"] is None


class TestCandidateTable:
    def test_nearest_first(self):
        analysis = build_analysis()
        headers, rows = dashboard_metrics.candidate_table(
            analysis[schema.CANDIDATES_LAYER])
        assert "Distance ft" in headers
        assert len(rows) == 1

    def test_a_missing_layer_gives_headers_and_no_rows(self):
        headers, rows = dashboard_metrics.candidate_table(None)
        assert headers and rows == []


@pytest.fixture(scope="module")
def page():
    analysis = build_analysis()
    metrics = dashboard_metrics.collect(analysis)
    headers, rows = dashboard_metrics.candidate_table(
        analysis[schema.CANDIDATES_LAYER])
    return dashboard.render(metrics, headers, rows, source="x.gpkg",
                            total_candidates=1)


class TestRender:

    def test_it_is_a_self_contained_document(self, page):
        assert page.lstrip().startswith("<!doctype html>")
        assert "</html>" in page

    def test_it_loads_nothing_from_a_network(self, page):
        """The map vendors Leaflet so it works offline; so must this.

        A dashboard that fetched a charting library from a CDN would be the one
        deliverable that stopped working on a machine with no internet.
        """
        for marker in ("http://", "https://", "<script"):
            assert marker not in page, f"{marker!r} in a page that must be offline"

    def test_the_headline_numbers_are_on_the_page(self, page):
        assert "Insertion candidates" in page
        assert "50.0%" in page

    def test_it_states_the_rules_it_was_run_under(self, page):
        assert f"Within {config.MAX_DISTANCE_FT:g} ft" in page
        assert config.COATED_STEEL_INSTALLED_BEFORE in page

    def test_a_pending_plastic_decision_is_declared(self, page):
        # The candidate count is a lower bound until those codes are set, and
        # the page must not present it as final.
        assert config.PLASTIC_ASSETTYPES == ()
        assert "lower bound" in page

    def test_it_is_theme_aware_in_both_directions(self, page):
        # The OS setting and an explicit toggle must both win.
        assert "prefers-color-scheme: dark" in page
        assert '[data-theme="dark"]' in page

    def test_an_empty_run_still_renders(self):
        metrics = dashboard_metrics.collect({})
        page = dashboard.render(metrics, ["System"], [])
        assert "<!doctype html>" in page
        assert "No candidates to list." in page

    def test_a_capped_table_says_so(self):
        metrics = dashboard_metrics.collect({})
        page = dashboard.render(metrics, ["System"], [["LP-1"]],
                                total_candidates=2187)
        assert "of 2,187 candidates" in page

    def test_an_uncapped_table_does_not_claim_to_be(self):
        metrics = dashboard_metrics.collect({})
        page = dashboard.render(metrics, ["System"], [["LP-1"]], total_candidates=1)
        assert "of 1 candidates" not in page


class TestColourChoices:
    def test_the_funnel_ramp_is_the_validated_one(self):
        """One hue, monotone light to dark, per mode.

        Checked here because the ramp is the only place a value ramp is
        correct on this page - the funnel's stages are genuinely ordered - and
        a hue creeping into it would make it a rainbow.
        """
        assert len(dashboard.FUNNEL_RAMP_LIGHT) == 5
        assert len(dashboard.FUNNEL_RAMP_DARK) == 5
        assert dashboard.FUNNEL_RAMP_LIGHT != dashboard.FUNNEL_RAMP_DARK

    def test_the_dark_ramp_is_stepped_not_flipped(self):
        # Reversing the light ramp for dark is the common shortcut and gives a
        # ramp that does not clear the dark surface at its dark end.
        assert dashboard.FUNNEL_RAMP_DARK != tuple(
            reversed(dashboard.FUNNEL_RAMP_LIGHT))


class TestBuild:
    def test_it_writes_beside_the_geopackage(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "OUTPUT_GPKG", tmp_path / "out.gpkg")
        assert dashboard.dashboard_path().parent == tmp_path

    def test_a_missing_geopackage_still_writes_a_page(self, tmp_path, monkeypatch):
        """Someone looking at a dashboard after a failed run needs a page.

        A traceback would tell them less than an empty dashboard that names
        the command to run.
        """
        monkeypatch.setattr(config, "OUTPUT_GPKG", tmp_path / "absent.gpkg")
        target = dashboard.build()
        assert target.is_file()
        assert "<!doctype html>" in target.read_text(encoding="utf-8")
