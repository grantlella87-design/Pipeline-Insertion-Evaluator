"""Tests for the near analysis, the connection path and the final selection.

The final test is the deliverable: `DISTANCE_FT <= 50 AND NEAREST_EP_PRESSURE >=
SYSTEM_PRESSURE`. These pin both halves, including the boundary values, and the
statuses that say why a system did not pass - a candidate list that cannot
explain its exclusions cannot be reviewed.
"""
import pytest
from shapely.geometry import LineString

from pipelineinsertion import config, nearest, schema, systems


def make_systems(rows, crs="EPSG:2249"):
    """Dissolved systems of the shape `analyse` expects.

    Each row is (system_id, bucket, pressure, units, geometry).
    """
    import geopandas as gpd
    import pandas as pd

    from pipelineinsertion import pressure as pressure_module

    columns = [schema.SYSTEM_ID, schema.PRESSURE_BUCKET, schema.SYSTEM_PRESSURE,
               schema.SYSTEM_PRESSURE_PSI, schema.SYSTEM_PRESSURE_UNITS,
               schema.MAIN_COUNT, schema.LENGTH_FT, schema.SOURCE_IDS]
    records = []
    geometries = []
    for system_id, bucket, value, units, geometry in rows:
        records.append({
            schema.SYSTEM_ID: system_id,
            schema.PRESSURE_BUCKET: bucket,
            schema.SYSTEM_PRESSURE: float(value),
            schema.SYSTEM_PRESSURE_PSI: pressure_module.to_psi(value, units),
            schema.SYSTEM_PRESSURE_UNITS: units,
            schema.MAIN_COUNT: 1,
            schema.LENGTH_FT: round(geometry.length, 2),
            schema.SOURCE_IDS: "{%s}|%s" % (system_id, system_id.lower()),
        })
        geometries.append(geometry)
    frame = pd.DataFrame(records, columns=columns)
    return gpd.GeoDataFrame(frame, geometry=geometries, crs=crs)


LOWER = config.BUCKET_LOWER
OTHER = config.BUCKET_OTHER
WC = config.PRESSURE_UNIT_WC
PSI = config.PRESSURE_UNIT_PSI

# A candidate along y=0, and targets placed a known distance away in y.
CANDIDATE = LineString([(0, 0), (100, 0)])


def target_at(distance, x0=0, x1=100):
    return LineString([(x0, distance), (x1, distance)])


class TestCandidateStatus:
    def test_inside_distance_and_pressure_passes(self):
        assert nearest.candidate_status(30.0, 2.0, 5.0) == (
            True, nearest.STATUS_CANDIDATE)

    def test_distance_boundary_is_inclusive(self):
        # "DISTANCE_FT <= 50": 50 ft exactly is a candidate.
        assert nearest.candidate_status(50.0, 2.0, 5.0)[0] is True
        assert nearest.candidate_status(50.01, 2.0, 5.0)[0] is False

    def test_pressure_boundary_is_inclusive(self):
        # "NEAREST_EP_PRESSURE >= SYSTEM_PRESSURE": equal pressures qualify.
        assert nearest.candidate_status(10.0, 5.0, 5.0)[0] is True

    def test_target_below_candidate_pressure_fails(self):
        passed, status = nearest.candidate_status(10.0, 5.0, 3.0)
        assert passed is False
        assert status == nearest.STATUS_TARGET_PRESSURE_TOO_LOW

    def test_no_target_at_all(self):
        passed, status = nearest.candidate_status(None, 2.0, None)
        assert passed is False
        assert status == nearest.STATUS_NO_TARGET_IN_RANGE

    def test_too_far_is_reported_before_a_pressure_problem(self):
        # A system whose nearest target is a mile away and at the wrong
        # pressure is reported as too far: moving the pressure would not help.
        passed, status = nearest.candidate_status(5000.0, 5.0, 1.0)
        assert passed is False
        assert status == nearest.STATUS_TOO_FAR

    def test_uncomparable_pressure_is_its_own_status(self):
        passed, status = nearest.candidate_status(10.0, None, 5.0)
        assert passed is False
        assert status == nearest.STATUS_PRESSURE_NOT_COMPARABLE

    def test_threshold_comes_from_config(self, restore_config):
        assert nearest.candidate_status(80.0, 2.0, 5.0)[0] is False
        restore_config("MAX_DISTANCE_FT", 100.0)
        assert nearest.candidate_status(80.0, 2.0, 5.0)[0] is True

    def test_explicit_threshold_overrides_config(self):
        assert nearest.candidate_status(80.0, 2.0, 5.0, max_distance_ft=100.0)[0] is True


class TestConnectionPath:
    def test_builds_a_two_point_line(self):
        line = nearest.connection_path(0, 0, 0, 30)
        assert line.geom_type == "LineString"
        assert line.length == pytest.approx(30.0)
        assert list(line.coords) == [(0.0, 0.0), (0.0, 30.0)]

    def test_zero_length_path_is_dropped(self):
        # Two systems that touch produce identical endpoints, and a zero-length
        # LineString is rejected by some GeoPackage readers and drawn as
        # nothing by the rest.
        assert nearest.connection_path(5, 5, 5, 5) is None

    def test_missing_coordinate_is_none(self):
        assert nearest.connection_path(0, 0, None, 30) is None


class TestNearResult:
    def test_finds_the_nearer_of_two_targets(self):
        targets = [
            ("FAR", target_at(400), 20.0, 20.0, PSI),
            ("NEAR", target_at(30), 10.0, 10.0, PSI),
        ]
        found = nearest.near_result(CANDIDATE, targets)
        assert found[schema.NEAREST_EP_ID] == "NEAR"
        assert found[schema.DISTANCE_FT] == pytest.approx(30.0)

    def test_captures_both_ends_of_the_shortest_path(self):
        found = nearest.near_result(CANDIDATE, [("T", target_at(30), 10.0, 10.0, PSI)])
        assert found[schema.NEAR_Y] == pytest.approx(30.0)
        assert found[schema.FROM_Y] == pytest.approx(0.0)

    def test_target_beyond_the_search_limit_is_not_returned(self, restore_config):
        restore_config("NEAR_SEARCH_LIMIT_FT", 100.0)
        assert nearest.near_result(
            CANDIDATE, [("T", target_at(5000), 10.0, 10.0, PSI)]) is None

    def test_no_targets_is_none(self):
        assert nearest.near_result(CANDIDATE, []) is None


class TestAnalyse:
    def test_a_qualifying_system_becomes_a_candidate(self):
        lower = make_systems([("LP1", LOWER, 30, WC, CANDIDATE)])
        other = make_systems([("OP1", OTHER, 20, PSI, target_at(30))])

        near, paths, candidates = nearest.analyse(lower, other)

        assert len(near) == 1 and len(candidates) == 1
        row = near.iloc[0]
        assert row[schema.NEAREST_EP_ID] == "OP1"
        assert row[schema.DISTANCE_FT] == pytest.approx(30.0)
        assert row[schema.CANDIDATE_STATUS] == nearest.STATUS_CANDIDATE
        assert len(paths) == 1
        assert paths.iloc[0].geometry.length == pytest.approx(30.0)

    def test_water_column_candidate_qualifies_against_a_psi_target(self):
        """The comparison the README's raw numeric test would get wrong.

        The candidate is 55" WC, about 2 PSI. The target is 5 PSI, genuinely
        above it. Compared as recorded numbers - 5 >= 55 - it would be dropped.
        """
        lower = make_systems([("LP1", LOWER, 55, WC, CANDIDATE)])
        other = make_systems([("OP1", OTHER, 5, PSI, target_at(30))])

        near, _, candidates = nearest.analyse(lower, other)

        assert near.iloc[0][schema.SYSTEM_PRESSURE] == 55.0     # recorded value kept
        assert near.iloc[0][schema.NEAREST_EP_PRESSURE] == 5.0  # recorded value kept
        assert len(candidates) == 1

    def test_every_system_gets_a_row_whether_it_passed_or_not(self):
        # A candidate list that only holds the passes cannot be checked.
        lower = make_systems([
            ("LP1", LOWER, 30, WC, CANDIDATE),
            ("LP2", LOWER, 30, WC, LineString([(0, 900), (100, 900)])),
        ])
        other = make_systems([("OP1", OTHER, 20, PSI, target_at(30))])

        near, _, candidates = nearest.analyse(lower, other)

        assert len(near) == 2
        assert len(candidates) == 1
        statuses = dict(zip(near[schema.SYSTEM_ID], near[schema.CANDIDATE_STATUS]))
        assert statuses["LP1"] == nearest.STATUS_CANDIDATE
        assert statuses["LP2"] == nearest.STATUS_TOO_FAR

    def test_target_at_lower_pressure_is_excluded_with_a_reason(self):
        lower = make_systems([("LP1", LOWER, 2, PSI, CANDIDATE)])
        other = make_systems([("OP1", OTHER, 30, WC, target_at(30))])  # ~1.08 PSI

        near, _, candidates = nearest.analyse(lower, other)

        assert len(candidates) == 0
        assert near.iloc[0][schema.CANDIDATE_STATUS] == (
            nearest.STATUS_TARGET_PRESSURE_TOO_LOW)

    def test_no_targets_at_all(self):
        lower = make_systems([("LP1", LOWER, 30, WC, CANDIDATE)])
        other = make_systems([])

        near, paths, candidates = nearest.analyse(lower, other)

        assert len(near) == 1 and len(candidates) == 0 and len(paths) == 0
        assert near.iloc[0][schema.CANDIDATE_STATUS] == (
            nearest.STATUS_NO_TARGET_IN_RANGE)

    def test_paths_carry_the_readme_fields(self):
        lower = make_systems([("LP1", LOWER, 30, WC, CANDIDATE)])
        other = make_systems([("OP1", OTHER, 20, PSI, target_at(30))])

        _, paths, _ = nearest.analyse(lower, other)

        for name in schema.INSERTION_PATH_FIELDS:
            assert name in paths.columns

    def test_near_table_carries_the_readme_fields(self):
        lower = make_systems([("LP1", LOWER, 30, WC, CANDIDATE)])
        other = make_systems([("OP1", OTHER, 20, PSI, target_at(30))])

        near, _, _ = nearest.analyse(lower, other)

        for name in schema.NEAR_OUTPUT_FIELDS:
            assert name in near.columns

    def test_source_ids_survive_to_the_candidate_layer(self):
        # Traceability is the point of the deliverable: a candidate on a map has
        # to lead back to the records it came from.
        lower = make_systems([("LP1", LOWER, 30, WC, CANDIDATE)])
        other = make_systems([("OP1", OTHER, 20, PSI, target_at(30))])

        _, _, candidates = nearest.analyse(lower, other)

        assert candidates.iloc[0][schema.SOURCE_IDS] == "{LP1}|lp1"

    def test_crs_is_preserved(self):
        lower = make_systems([("LP1", LOWER, 30, WC, CANDIDATE)])
        other = make_systems([("OP1", OTHER, 20, PSI, target_at(30))])

        near, paths, candidates = nearest.analyse(lower, other)

        for frame in (near, paths, candidates):
            assert frame.crs == lower.crs


class TestEndToEnd:
    def test_dissolve_then_analyse(self):
        """The whole analysis half, from classified mains to candidates."""
        import geopandas as gpd

        rows = [
            # Two contiguous Lower Pressure mains, and a target 30 ft away.
            ("{A}", "a", LOWER, 30, WC, LineString([(0, 0), (50, 0)])),
            ("{B}", "b", LOWER, 30, WC, LineString([(50, 0), (100, 0)])),
            ("{T}", "t", OTHER, 20, PSI, LineString([(0, 30), (100, 30)])),
        ]
        from pipelineinsertion import pressure as pressure_module

        frame = gpd.GeoDataFrame(
            [{"GLOBALID": g, "legacyid": l, schema.PRESSURE_BUCKET: b,
              schema.PRESSURE: float(p), schema.PRESSURE_UNITS: u,
              schema.PRESSURE_PSI: pressure_module.to_psi(p, u)}
             for g, l, b, p, u, _ in rows],
            geometry=[r[5] for r in rows], crs="EPSG:2249")

        lower = systems.dissolve(
            frame[frame[schema.PRESSURE_BUCKET] == LOWER], "GLOBALID", "legacyid")
        other = systems.dissolve(
            frame[frame[schema.PRESSURE_BUCKET] == OTHER], "GLOBALID", "legacyid")

        assert len(lower) == 1 and lower.iloc[0][schema.MAIN_COUNT] == 2

        near, paths, candidates = nearest.analyse(lower, other)

        assert len(candidates) == 1
        assert candidates.iloc[0][schema.SOURCE_IDS] == "{A}|a;{B}|b"
        assert candidates.iloc[0][schema.DISTANCE_FT] == pytest.approx(30.0)
        assert len(paths) == 1
