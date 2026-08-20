"""Nearest Other Pressure system, connection path, and final candidate test.

For each dissolved Lower Pressure system this finds the nearest Other Pressure
system, the shortest distance between them, the point on the target where that
distance is measured, and the target's pressure. The connection path layer is
the segment between the two nearest points - the shortest possible insertion
tie-in, drawn so it can be looked at.

A system is a candidate when both of the README's final tests pass:

    DISTANCE_FT <= 50
    NEAREST_EP_PRESSURE >= SYSTEM_PRESSURE

The second is applied in PSI on both sides rather than on the raw recorded
numbers - see the note in `pressure.py` on why comparing a water-column
candidate against a PSI target as plain numbers drops candidates that qualify.

Every Lower Pressure system gets a row in the near result, passed or not, with
a status saying which test it failed. A candidate list that only holds the
passes cannot be checked: "12 candidates" and "12 candidates out of 4,000
examined, 3,100 of them with nothing within 50 ft" are different reports, and
only the second one shows when the analysis has gone wrong.
"""
# Absolute imports with this path setup, rather than relative imports, so the
# module also works when loaded by file path or run directly - not only when
# imported as a package member.
import os as _os
import sys as _sys

_PACKAGE_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PACKAGE_PARENT not in _sys.path:
    _sys.path.insert(0, _PACKAGE_PARENT)

from shapely.geometry import LineString
from shapely.ops import nearest_points
from shapely.strtree import STRtree

from pipelineinsertion import config, pressure, schema
from pipelineinsertion.output import log

# Why a Lower Pressure system is or is not a candidate.
STATUS_CANDIDATE = "candidate"
STATUS_NO_TARGET_IN_RANGE = "no_other_pressure_system_within_search_limit"
STATUS_TOO_FAR = "nearest_system_beyond_max_distance"
STATUS_TARGET_PRESSURE_TOO_LOW = "nearest_system_pressure_below_candidate"
STATUS_PRESSURE_NOT_COMPARABLE = "pressure_not_comparable_between_systems"


def near_result(candidate_geometry, targets, tree=None):
    """The nearest target to one candidate.

    `targets` is a list of (id, geometry, pressure, pressure_psi, units).
    Returns a dict of near fields, or None when nothing is within
    `config.NEAR_SEARCH_LIMIT_FT`.

    The search limit exists so this stays a bounded query. Without it, every
    candidate in the state matches something - an isolated system's "nearest"
    target is thirty miles away, which is not a near result, it is noise that
    has to be filtered out of the output afterwards.
    """
    if candidate_geometry is None or candidate_geometry.is_empty or not targets:
        return None

    geometries = [entry[1] for entry in targets]
    tree = tree if tree is not None else STRtree(geometries)

    index = tree.nearest(candidate_geometry)
    if index is None:
        return None
    index = int(index)

    target_id, target_geometry, target_pressure, target_psi, target_units = targets[index]
    distance = candidate_geometry.distance(target_geometry)
    if distance > config.NEAR_SEARCH_LIMIT_FT:
        return None

    from_point, to_point = nearest_points(candidate_geometry, target_geometry)
    return {
        schema.NEAREST_EP_ID: target_id,
        schema.NEAREST_EP_PRESSURE: target_pressure,
        schema.NEAREST_EP_PRESSURE_PSI: target_psi,
        schema.NEAREST_EP_PRESSURE_UNITS: target_units,
        schema.DISTANCE_FT: round(float(distance), 2),
        schema.NEAR_X: round(float(to_point.x), 3),
        schema.NEAR_Y: round(float(to_point.y), 3),
        schema.FROM_X: round(float(from_point.x), 3),
        schema.FROM_Y: round(float(from_point.y), 3),
    }


def candidate_status(distance_ft, candidate_psi, target_psi,
                     max_distance_ft=None):
    """(is_candidate, status) for one near result.

    Distance is tested first so the status names the more actionable problem:
    a system whose nearest target is a mile away and at the wrong pressure is
    reported as too far, because moving the pressure would not help it.
    """
    max_distance_ft = (config.MAX_DISTANCE_FT if max_distance_ft is None
                       else max_distance_ft)

    if distance_ft is None:
        return False, STATUS_NO_TARGET_IN_RANGE
    if distance_ft > max_distance_ft:
        return False, STATUS_TOO_FAR
    if candidate_psi is None or target_psi is None:
        return False, STATUS_PRESSURE_NOT_COMPARABLE
    if not pressure.target_serves_candidate(candidate_psi, target_psi):
        return False, STATUS_TARGET_PRESSURE_TOO_LOW
    return True, STATUS_CANDIDATE


def connection_path(from_x, from_y, near_x, near_y):
    """The shortest path from a candidate to its target, as a line.

    Returns None for a zero-length path. Two systems that touch produce
    identical endpoints, and a zero-length LineString is rejected by some
    GeoPackage readers and drawn as nothing by the rest - so the pair is
    dropped from the path layer while keeping its row in the near table.
    """
    if None in (from_x, from_y, near_x, near_y):
        return None
    if (from_x, from_y) == (near_x, near_y):
        return None
    return LineString([(from_x, from_y), (near_x, near_y)])


def analyse(lower_systems, other_systems):
    """Run the near analysis over every Lower Pressure system.

    Returns (near_table, paths, candidates) as three GeoDataFrames:

        near_table  one row per Lower Pressure system, geometry = the system,
                    carrying its near result and candidate status
        paths       the connection path lines, for systems that had a target
        candidates  the systems that passed both tests, geometry = the system

    All three share SYSTEM_ID, so they join back to each other and to the
    dissolved system layers.
    """
    import geopandas as gpd
    import pandas as pd

    schema.require(lower_systems, [schema.SYSTEM_ID, schema.SYSTEM_PRESSURE,
                                   schema.SYSTEM_PRESSURE_PSI, schema.SOURCE_IDS],
                   "the Lower Pressure systems")

    targets = _target_list(other_systems)
    tree = STRtree([entry[1] for entry in targets]) if targets else None
    if not targets:
        log("No Other Pressure systems, so no Lower Pressure system can have a "
            "target. Every system will be reported as having nothing in range.")

    rows = []
    for _, system in lower_systems.iterrows():
        row = {
            schema.SYSTEM_ID: system[schema.SYSTEM_ID],
            schema.PRESSURE_BUCKET: system[schema.PRESSURE_BUCKET],
            schema.SYSTEM_PRESSURE: system[schema.SYSTEM_PRESSURE],
            schema.SYSTEM_PRESSURE_PSI: system[schema.SYSTEM_PRESSURE_PSI],
            schema.SYSTEM_PRESSURE_UNITS: system[schema.SYSTEM_PRESSURE_UNITS],
            schema.MAIN_COUNT: system[schema.MAIN_COUNT],
            schema.LENGTH_FT: system[schema.LENGTH_FT],
            schema.SOURCE_IDS: system[schema.SOURCE_IDS],
            schema.NEAREST_EP_ID: "",
            schema.NEAREST_EP_PRESSURE: None,
            schema.NEAREST_EP_PRESSURE_PSI: None,
            schema.NEAREST_EP_PRESSURE_UNITS: None,
            schema.DISTANCE_FT: None,
            schema.NEAR_X: None,
            schema.NEAR_Y: None,
            schema.FROM_X: None,
            schema.FROM_Y: None,
            "geometry": system.geometry,
        }
        found = near_result(system.geometry, targets, tree) if targets else None
        if found:
            row.update(found)

        is_candidate, status = candidate_status(
            row[schema.DISTANCE_FT],
            _optional_float(system[schema.SYSTEM_PRESSURE_PSI]),
            _optional_float(row[schema.NEAREST_EP_PRESSURE_PSI]),
        )
        row[schema.IS_CANDIDATE] = bool(is_candidate)
        row[schema.CANDIDATE_STATUS] = status
        rows.append(row)

    if not rows:
        empty = _empty_near(lower_systems.crs)
        return empty, _empty_paths(lower_systems.crs), empty

    near_table = gpd.GeoDataFrame(
        pd.DataFrame(rows), geometry="geometry", crs=lower_systems.crs)

    paths = _paths_from(near_table)
    candidates = near_table[near_table[schema.IS_CANDIDATE]].copy()

    _report(near_table)
    return near_table, paths, candidates


def _target_list(other_systems):
    """(id, geometry, pressure, pressure_psi, units) for each target system."""
    if other_systems is None or len(other_systems) == 0:
        return []
    schema.require(other_systems, [schema.SYSTEM_ID, schema.SYSTEM_PRESSURE,
                                   schema.SYSTEM_PRESSURE_PSI],
                   "the Other Pressure systems")
    targets = []
    for _, system in other_systems.iterrows():
        geometry = system.geometry
        if geometry is None or geometry.is_empty:
            continue
        targets.append((
            system[schema.SYSTEM_ID],
            geometry,
            _optional_float(system[schema.SYSTEM_PRESSURE]),
            _optional_float(system[schema.SYSTEM_PRESSURE_PSI]),
            system[schema.SYSTEM_PRESSURE_UNITS],
        ))
    return targets


def _paths_from(near_table):
    import geopandas as gpd
    import pandas as pd

    rows = []
    for _, near in near_table.iterrows():
        line = connection_path(near[schema.FROM_X], near[schema.FROM_Y],
                               near[schema.NEAR_X], near[schema.NEAR_Y])
        if line is None:
            continue
        row = {name: near[name] for name in schema.INSERTION_PATH_FIELDS}
        row[schema.IS_CANDIDATE] = near[schema.IS_CANDIDATE]
        row[schema.CANDIDATE_STATUS] = near[schema.CANDIDATE_STATUS]
        row["geometry"] = line
        rows.append(row)

    if not rows:
        return _empty_paths(near_table.crs)
    return gpd.GeoDataFrame(pd.DataFrame(rows), geometry="geometry",
                            crs=near_table.crs)


def _report(near_table):
    counts = near_table[schema.CANDIDATE_STATUS].value_counts().to_dict()
    log(f"Examined {len(near_table):,} Lower Pressure systems:")
    for status in sorted(counts, key=lambda name: -counts[name]):
        log(f"  {counts[status]:>8,}  {status}")


def _optional_float(value):
    try:
        if value is None:
            return None
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value != value:  # NaN, which pandas puts in for a missing float
        return None
    return value


def _empty_near(crs):
    import geopandas as gpd
    import pandas as pd

    columns = [
        schema.SYSTEM_ID, schema.PRESSURE_BUCKET, schema.SYSTEM_PRESSURE,
        schema.SYSTEM_PRESSURE_PSI, schema.SYSTEM_PRESSURE_UNITS,
        schema.MAIN_COUNT, schema.LENGTH_FT, schema.SOURCE_IDS,
        schema.NEAREST_EP_ID, schema.NEAREST_EP_PRESSURE,
        schema.NEAREST_EP_PRESSURE_PSI, schema.NEAREST_EP_PRESSURE_UNITS,
        schema.DISTANCE_FT, schema.NEAR_X, schema.NEAR_Y,
        schema.FROM_X, schema.FROM_Y,
        schema.IS_CANDIDATE, schema.CANDIDATE_STATUS,
    ]
    frame = pd.DataFrame({name: [] for name in columns})
    frame["geometry"] = []
    return gpd.GeoDataFrame(frame, geometry="geometry", crs=crs)


def _empty_paths(crs):
    import geopandas as gpd
    import pandas as pd

    columns = list(schema.INSERTION_PATH_FIELDS) + [
        schema.IS_CANDIDATE, schema.CANDIDATE_STATUS]
    frame = pd.DataFrame({name: [] for name in columns})
    frame["geometry"] = []
    return gpd.GeoDataFrame(frame, geometry="geometry", crs=crs)
