"""Tests for dissolving contiguous mains into systems.

The behaviour worth protecting here is that connectivity is respected. A
`dissolve(by=[pressure, bucket])` would pass a test that only counted features
and only fails when you check that two mains at the same pressure on opposite
sides of the state did not become one system - which is exactly the mistake
these tests exist to catch.
"""
import pytest
from shapely.geometry import LineString, MultiLineString

from pipelineinsertion import config, schema, systems


def make_frame(rows, crs="EPSG:2249"):
    """A classified frame of the shape `dissolve` expects.

    Each row is (guid, legacy, bucket, pressure, units, geometry).
    """
    import geopandas as gpd
    import pandas as pd

    from pipelineinsertion import pressure as pressure_module

    columns = ["GLOBALID", "legacyid", schema.PRESSURE_BUCKET, schema.PRESSURE,
               schema.PRESSURE_UNITS, schema.PRESSURE_PSI]
    records = []
    geometries = []
    for guid, legacy, bucket, value, units, geometry in rows:
        records.append({
            "GLOBALID": guid,
            "legacyid": legacy,
            schema.PRESSURE_BUCKET: bucket,
            schema.PRESSURE: float(value),
            schema.PRESSURE_UNITS: units,
            schema.PRESSURE_PSI: pressure_module.to_psi(value, units),
        })
        geometries.append(geometry)
    # Columns are declared even when there are no rows: an empty frame reaching
    # dissolve in the real workflow is a filtered slice of the classified frame,
    # so it still carries every column. A bare frame with only a geometry column
    # is not a case the workflow can produce, and building one here would test
    # the helper rather than the code.
    frame = pd.DataFrame(records, columns=columns)
    return gpd.GeoDataFrame(frame, geometry=geometries, crs=crs)


LOWER = config.BUCKET_LOWER
OTHER = config.BUCKET_OTHER
WC = config.PRESSURE_UNIT_WC
PSI = config.PRESSURE_UNIT_PSI


class TestSourceIds:
    def test_format_matches_the_readme(self):
        text = systems.source_ids([
            ("{F6D95C58-43AB-4A11-BD17-102A65E9D3C2}", "123456"),
            ("{31A613FC-614C-45B3-B1B9-8AF378AA5D44}", "789456"),
        ])
        assert text == (
            "{31A613FC-614C-45B3-B1B9-8AF378AA5D44}|789456;"
            "{F6D95C58-43AB-4A11-BD17-102A65E9D3C2}|123456")

    def test_guids_are_braced_however_they_arrived(self):
        # One endpoint braces them and another does not; a traceability field
        # that is only sometimes braced cannot be joined on.
        assert systems.source_ids([("ABC", "1")]) == "{ABC}|1"
        assert systems.source_ids([("{ABC}", "1")]) == "{ABC}|1"

    def test_sorted_and_deduplicated_so_the_string_is_stable(self):
        first = systems.source_ids([("{B}", "2"), ("{A}", "1")])
        second = systems.source_ids([("{A}", "1"), ("{B}", "2"), ("{A}", "1")])
        assert first == second == "{A}|1;{B}|2"

    def test_missing_legacy_id_leaves_an_empty_half(self):
        assert systems.source_ids([("{A}", None)]) == "{A}|"

    def test_a_main_with_no_guid_is_not_claimed(self):
        # It cannot be traced back, so it is left out of the traceability field
        # rather than appearing as an empty reference.
        assert systems.source_ids([("{A}", "1"), ("", "2"), (None, "3")]) == "{A}|1"


class TestSystemId:
    def test_prefix_names_the_bucket(self):
        assert systems.system_id(LOWER, "{A}|1").startswith("LP-")
        assert systems.system_id(OTHER, "{A}|1").startswith("OP-")

    def test_stable_for_the_same_membership(self):
        assert systems.system_id(LOWER, "{A}|1") == systems.system_id(LOWER, "{A}|1")

    def test_changes_when_membership_changes(self):
        assert systems.system_id(LOWER, "{A}|1") != systems.system_id(LOWER, "{A}|1;{B}|2")

    def test_not_a_sequence_number(self):
        # A sequence depends on row order, so an unrelated main added upstream
        # would renumber every system after it and make two runs undiffable.
        first = systems.system_id(LOWER, "{A}|1")
        assert not first.endswith("-1")
        assert len(first) > len("LP-1")


class TestConnectedComponents:
    def test_touching_lines_are_one_component(self):
        parts = [LineString([(0, 0), (10, 0)]), LineString([(10, 0), (20, 0)])]
        assert sorted(map(sorted, systems.connected_components(parts, 0.1))) == [[0, 1]]

    def test_detached_lines_are_separate_components(self):
        parts = [LineString([(0, 0), (10, 0)]), LineString([(500, 500), (510, 500)])]
        components = systems.connected_components(parts, 0.1)
        assert sorted(map(sorted, components)) == [[0], [1]]

    def test_a_lone_line_is_a_component_of_one(self):
        # A system of one main is still a system; dropping it would remove real
        # candidates.
        assert systems.connected_components([LineString([(0, 0), (1, 0)])], 0.1) == [[0]]

    def test_every_index_appears_exactly_once(self):
        parts = [LineString([(i, 0), (i + 1, 0)]) for i in range(6)]
        parts.append(LineString([(90, 90), (91, 90)]))
        seen = sorted(i for component in systems.connected_components(parts, 0.1)
                      for i in component)
        assert seen == list(range(7))

    def test_tolerance_bridges_a_digitising_gap(self):
        # Mains digitised a hundredth of a foot apart are one physical system;
        # exact coordinate equality split them into two.
        parts = [LineString([(0, 0), (10, 0)]), LineString([(10.01, 0), (20, 0)])]
        assert len(systems.connected_components(parts, 0.1)) == 1
        assert len(systems.connected_components(parts, 0.001)) == 2

    def test_a_long_chain_does_not_recurse(self):
        # Union-find with recursive path compression hit the recursion limit on
        # a real extract; a distribution system is a very long chain.
        parts = [LineString([(i, 0), (i + 1, 0)]) for i in range(2000)]
        assert len(systems.connected_components(parts, 0.1)) == 1

    def test_empty_input(self):
        assert systems.connected_components([], 0.1) == []


class TestDissolve:
    def test_contiguous_mains_at_one_pressure_become_one_system(self):
        frame = make_frame([
            ("{A}", "a", LOWER, 30, WC, LineString([(0, 0), (10, 0)])),
            ("{B}", "b", LOWER, 30, WC, LineString([(10, 0), (20, 0)])),
        ])
        result = systems.dissolve(frame, "GLOBALID", "legacyid")
        assert len(result) == 1
        assert result.iloc[0][schema.MAIN_COUNT] == 2
        assert result.iloc[0][schema.SOURCE_IDS] == "{A}|a;{B}|b"
        assert result.iloc[0][schema.LENGTH_FT] == pytest.approx(20.0)

    def test_detached_mains_at_one_pressure_stay_separate(self):
        """The defect a plain groupby-dissolve would introduce.

        Both mains are Lower Pressure at 30 WC, so a dissolve on those two
        columns merges them into one multipart feature spanning the state. That
        feature has no meaningful distance to anything, so the near analysis
        downstream would be measuring against a system that is everywhere.
        """
        frame = make_frame([
            ("{A}", "a", LOWER, 30, WC, LineString([(0, 0), (10, 0)])),
            ("{B}", "b", LOWER, 30, WC, LineString([(9000, 9000), (9010, 9000)])),
        ])
        result = systems.dissolve(frame, "GLOBALID", "legacyid")
        assert len(result) == 2
        assert set(result[schema.MAIN_COUNT]) == {1}

    def test_touching_mains_at_different_pressures_stay_separate(self):
        frame = make_frame([
            ("{A}", "a", LOWER, 30, WC, LineString([(0, 0), (10, 0)])),
            ("{B}", "b", LOWER, 40, WC, LineString([(10, 0), (20, 0)])),
        ])
        result = systems.dissolve(frame, "GLOBALID", "legacyid")
        assert len(result) == 2
        assert sorted(result[schema.SYSTEM_PRESSURE]) == [30.0, 40.0]

    def test_touching_mains_in_different_buckets_stay_separate(self):
        frame = make_frame([
            ("{A}", "a", LOWER, 2, PSI, LineString([(0, 0), (10, 0)])),
            ("{B}", "b", OTHER, 2, PSI, LineString([(10, 0), (20, 0)])),
        ])
        # Same numeric pressure, different bucket: two systems.
        result = systems.dissolve(frame, "GLOBALID", "legacyid")
        assert len(result) == 2
        assert set(result[schema.PRESSURE_BUCKET]) == {LOWER, OTHER}

    def test_float_noise_does_not_split_a_system(self):
        # Two mains recorded at the same pressure can differ in the last bit
        # after an upstream unit conversion.
        frame = make_frame([
            ("{A}", "a", LOWER, 30.0, WC, LineString([(0, 0), (10, 0)])),
            ("{B}", "b", LOWER, 30.0 + 1e-12, WC, LineString([(10, 0), (20, 0)])),
        ])
        assert len(systems.dissolve(frame, "GLOBALID", "legacyid")) == 1

    def test_system_ids_are_unique(self):
        frame = make_frame([
            ("{A}", "a", LOWER, 30, WC, LineString([(0, 0), (10, 0)])),
            ("{B}", "b", LOWER, 30, WC, LineString([(500, 500), (510, 500)])),
            ("{C}", "c", OTHER, 20, PSI, LineString([(0, 40), (10, 40)])),
        ])
        result = systems.dissolve(frame, "GLOBALID", "legacyid")
        assert len(set(result[schema.SYSTEM_ID])) == len(result)

    def test_pressure_psi_is_carried_through(self):
        frame = make_frame([
            ("{A}", "a", LOWER, 30, WC, LineString([(0, 0), (10, 0)])),
        ])
        result = systems.dissolve(frame, "GLOBALID", "legacyid")
        assert result.iloc[0][schema.SYSTEM_PRESSURE] == 30.0
        assert result.iloc[0][schema.SYSTEM_PRESSURE_PSI] == pytest.approx(1.0827, abs=1e-4)

    def test_empty_input_gives_an_empty_frame_with_the_right_columns(self):
        result = systems.dissolve(make_frame([]), "GLOBALID", "legacyid")
        assert len(result) == 0
        for column in (schema.SYSTEM_ID, schema.SOURCE_IDS, schema.PRESSURE_BUCKET):
            assert column in result.columns

    def test_missing_id_fields_do_not_raise(self):
        # A layer with no legacyid still has to produce systems.
        frame = make_frame([
            ("{A}", "a", LOWER, 30, WC, LineString([(0, 0), (10, 0)])),
        ]).drop(columns=["legacyid"])
        result = systems.dissolve(frame, "GLOBALID", "legacyid")
        assert len(result) == 1
        assert result.iloc[0][schema.SOURCE_IDS] == "{A}|"

    def test_a_missing_required_column_is_named(self):
        frame = make_frame([
            ("{A}", "a", LOWER, 30, WC, LineString([(0, 0), (10, 0)])),
        ]).drop(columns=[schema.PRESSURE_PSI])
        with pytest.raises(KeyError, match=schema.PRESSURE_PSI):
            systems.dissolve(frame, "GLOBALID", "legacyid")


class TestDissolveGeometries:
    def test_end_to_end_run_merges_into_one_line(self):
        merged = systems.dissolve_geometries([
            LineString([(0, 0), (10, 0)]), LineString([(10, 0), (20, 0)])])
        assert merged.geom_type == "LineString"
        assert merged.length == pytest.approx(20.0)

    def test_a_branch_stays_multipart(self):
        # linemerge only merges where the topology allows it; a tee is
        # legitimately a MultiLineString.
        merged = systems.dissolve_geometries([
            LineString([(0, 0), (10, 0)]),
            LineString([(10, 0), (20, 0)]),
            LineString([(10, 0), (10, 10)]),
        ])
        assert merged.geom_type == "MultiLineString"

    def test_empty_input_is_none(self):
        assert systems.dissolve_geometries([]) is None
        assert systems.dissolve_geometries([None]) is None


class TestMultipart:
    def test_linestring_becomes_multilinestring(self):
        # A GeoPackage layer holds one geometry type, so the mixture a dissolve
        # produces has to be normalised before it is written.
        result = systems.multipart(LineString([(0, 0), (1, 0)]))
        assert isinstance(result, MultiLineString)

    def test_multilinestring_passes_through(self):
        geometry = MultiLineString([[(0, 0), (1, 0)]])
        assert systems.multipart(geometry) is geometry

    def test_none_and_empty_are_none(self):
        assert systems.multipart(None) is None
        assert systems.multipart(LineString()) is None
