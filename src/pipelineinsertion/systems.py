"""Dissolving contiguous mains into pipeline systems, with traceability.

A "system" here is what the README asks for: a run of *contiguous connected*
mains that share an operating pressure and a pressure bucket. All three
conditions matter, and the usual one-line dissolve satisfies only two of them -
`GeoDataFrame.dissolve(by=[...])` merges every main with the same pressure
across the whole state into a single multipart feature, whether or not any of
them touch. That feature has no distance to anything, because it is everywhere,
so the near analysis downstream would be meaningless.

So connectivity is computed first: mains are grouped by (bucket, pressure), and
within each group the connected components are found and dissolved one at a
time. Two mains are connected when they come within
`config.CONNECT_TOLERANCE_FT` of each other. Exact coordinate equality was not
enough - mains digitised to within a hundredth of a foot of each other are one
physical system, and testing for equality split them into two.

Each system carries `SOURCE_IDS`, which is what makes a dissolved feature
arguable: every GLOBALID and legacy id that went into it, so an engineer can
get from a candidate on a map back to the records it came from.
"""
# Absolute imports with this path setup, rather than relative imports, so the
# module also works when loaded by file path or run directly - not only when
# imported as a package member.
import os as _os
import sys as _sys

_PACKAGE_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PACKAGE_PARENT not in _sys.path:
    _sys.path.insert(0, _PACKAGE_PARENT)

import hashlib

from shapely.geometry import MultiLineString
from shapely.ops import linemerge, unary_union
from shapely.strtree import STRtree

from pipelineinsertion import config, schema
from pipelineinsertion.fields import clean, normalize_key
from pipelineinsertion.output import detail, log

# Prefix on a SYSTEM_ID, by bucket. Which bucket a system is in is the first
# thing anyone wants to know from an id quoted in an email.
SYSTEM_ID_PREFIX = {
    config.BUCKET_LOWER: "LP",
    config.BUCKET_OTHER: "OP",
}
DEFAULT_SYSTEM_ID_PREFIX = "SYS"


def source_ids(guid_legacy_pairs):
    """The README's traceability string: {GUID}|LegacyID;{GUID}|LegacyID

    GUIDs are emitted braced whether or not the service delivered them that
    way, because the two endpoints this data comes from disagree and a
    traceability field that is only sometimes braced cannot be joined on.

    Pairs are sorted and de-duplicated, so the same set of mains always produces
    the same string - which is what lets `system_id` be a stable hash of it, and
    what makes two runs of the workflow diffable.
    """
    seen = {}
    for guid, legacy in guid_legacy_pairs:
        key = normalize_key(guid)
        if not key:
            # A main with no GLOBALID cannot be traced back, so it is not
            # claimed in the traceability field. It is still dissolved into the
            # system and still counted in MAIN_COUNT - dropping the geometry
            # would change the answer to hide a metadata gap.
            continue
        seen[key] = clean(legacy)
    return ";".join(f"{{{key}}}|{seen[key]}" for key in sorted(seen))


def system_id(bucket, source_ids_text):
    """A stable id for a system, derived from the mains that make it up.

    Deliberately not a sequence number. A sequence depends on row order, so
    re-running the workflow after an unrelated main was added renumbers every
    system downstream of it and makes two runs impossible to compare. Hashing
    the source ids means an id changes only when the system's own membership
    changes.
    """
    prefix = SYSTEM_ID_PREFIX.get(bucket, DEFAULT_SYSTEM_ID_PREFIX)
    digest = hashlib.sha1(source_ids_text.encode("utf-8")).hexdigest()[:10].upper()
    return f"{prefix}-{digest}"


def connected_components(geometries, tolerance):
    """Indices of `geometries` grouped into connected components.

    Connectivity is "within `tolerance`", not "touches": see the module
    docstring. The pairs to test come from an STRtree query on each geometry's
    envelope expanded by the tolerance, so this is a spatial-index sweep rather
    than the n-squared comparison the definition suggests.

    Returns a list of index lists. Every input index appears exactly once,
    including geometries that are connected to nothing - a lone main is a system
    of one, and dropping it would quietly remove real candidates.
    """
    count = len(geometries)
    if count == 0:
        return []

    parent = list(range(count))

    def find(index):
        # Path compression, iteratively. A long chain of mains - which is what a
        # distribution system is - recursed deep enough to hit the interpreter's
        # recursion limit on a real extract.
        root = index
        while parent[root] != root:
            root = parent[root]
        while parent[index] != root:
            parent[index], index = root, parent[index]
        return root

    def union(left, right):
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    tree = STRtree(geometries)
    for index, geometry in enumerate(geometries):
        if geometry is None or geometry.is_empty:
            continue
        # Query on the expanded envelope, then confirm with a real distance:
        # the tree answers on bounding boxes, and two mains whose boxes overlap
        # can be far apart.
        minx, miny, maxx, maxy = geometry.bounds
        search = _box(minx - tolerance, miny - tolerance,
                      maxx + tolerance, maxy + tolerance)
        for other_index in tree.query(search):
            other_index = int(other_index)
            if other_index <= index:
                # Every pair is offered twice; testing it once halves the work.
                continue
            other = geometries[other_index]
            if other is None or other.is_empty:
                continue
            if geometry.distance(other) <= tolerance:
                union(index, other_index)

    groups = {}
    for index in range(count):
        groups.setdefault(find(index), []).append(index)
    return list(groups.values())


def _box(minx, miny, maxx, maxy):
    from shapely.geometry import box

    return box(minx, miny, maxx, maxy)


def dissolve_geometries(geometries):
    """One geometry for a component, merged into as few lines as possible.

    `linemerge` is applied after the union so a run of mains that meet
    end-to-end becomes a single LineString rather than a bag of segments. It
    only merges where the topology allows it; a branching system legitimately
    stays a MultiLineString.
    """
    usable = [geom for geom in geometries if geom is not None and not geom.is_empty]
    if not usable:
        return None
    merged = unary_union(usable)
    if merged.is_empty:
        return None
    try:
        simplified = linemerge(merged)
    except (ValueError, AttributeError):
        # linemerge rejects anything that is not lines. A pressure layer should
        # not hold points or polygons, but one bad geometry should not end the
        # run - the union is still a correct answer, just a less tidy one.
        return merged
    if simplified.is_empty:
        return merged
    return simplified


def dissolve(gdf, guid_field, legacy_field):
    """Dissolve classified mains into systems.

    Expects the columns `classify` writes: PRESSURE_BUCKET, PRESSURE,
    PRESSURE_PSI and PRESSURE_UNITS. Returns a GeoDataFrame of systems with the
    columns named in `schema`, one row per connected component.

    Mains outside both buckets are not passed in; grouping on an empty bucket
    would dissolve every unclassified main in the state into one system.
    """
    import geopandas as gpd
    import pandas as pd

    schema.require(gdf, [schema.PRESSURE_BUCKET, schema.PRESSURE,
                         schema.PRESSURE_PSI, schema.PRESSURE_UNITS],
                   "the frame handed to dissolve")

    if len(gdf) == 0:
        return _empty_systems(gdf.crs)

    rows = []
    # Group on the bucket and the pressure exactly as the README specifies. The
    # pressure is rounded first: OPERATINGPRESSURE arrives as a float, and two
    # mains recorded at the same pressure can differ in the last bit after a
    # unit conversion upstream, which would put them in different groups and
    # split a system that is physically one.
    grouping = gdf.assign(
        _group_pressure=gdf[schema.PRESSURE].round(6),
    ).groupby([schema.PRESSURE_BUCKET, "_group_pressure"], dropna=False, sort=True)

    for (bucket, group_pressure), group in grouping:
        geometries = list(group.geometry.values)
        components = connected_components(geometries, config.CONNECT_TOLERANCE_FT)
        detail(f"{bucket} at {group_pressure}: {len(group):,} mains -> "
               f"{len(components):,} systems")

        positional = group.reset_index(drop=True)
        for component in components:
            member = positional.iloc[component]
            geometry = dissolve_geometries(list(member.geometry.values))
            if geometry is None:
                continue
            pairs = zip(
                member[guid_field] if guid_field in member.columns else [""] * len(member),
                member[legacy_field] if legacy_field in member.columns else [""] * len(member),
            )
            ids_text = source_ids(pairs)
            # A first pressure rather than the group key: the key was rounded
            # for grouping, and the output should carry the recorded value.
            first = member.iloc[0]
            rows.append({
                schema.SYSTEM_ID: system_id(bucket, ids_text),
                schema.PRESSURE_BUCKET: bucket,
                schema.SYSTEM_PRESSURE: float(first[schema.PRESSURE]),
                schema.SYSTEM_PRESSURE_PSI: _optional_float(first[schema.PRESSURE_PSI]),
                schema.SYSTEM_PRESSURE_UNITS: first[schema.PRESSURE_UNITS],
                schema.MAIN_COUNT: int(len(member)),
                schema.LENGTH_FT: round(float(geometry.length), 2),
                schema.SOURCE_IDS: ids_text,
                "geometry": geometry,
            })

    if not rows:
        return _empty_systems(gdf.crs)

    systems = gpd.GeoDataFrame(pd.DataFrame(rows), geometry="geometry", crs=gdf.crs)
    _warn_on_duplicate_ids(systems)
    log(f"Dissolved {len(gdf):,} mains into {len(systems):,} systems.")
    return systems


def _optional_float(value):
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _warn_on_duplicate_ids(systems):
    """A repeated SYSTEM_ID means two systems dissolved from the same mains.

    That is possible only when a main was placed in two groups, which would be a
    bug in the grouping rather than in the data - so it is reported loudly
    instead of being de-duplicated away.
    """
    from pipelineinsertion.output import warn

    duplicated = systems[schema.SYSTEM_ID].duplicated(keep=False)
    if duplicated.any():
        repeated = sorted(set(systems.loc[duplicated, schema.SYSTEM_ID]))
        warn(f"{len(repeated)} SYSTEM_IDs are not unique: {repeated[:5]}. "
             f"Two systems dissolved from the same source mains, which means "
             f"the grouping placed a main in more than one group.")


def _empty_systems(crs):
    import geopandas as gpd
    import pandas as pd

    columns = [
        schema.SYSTEM_ID, schema.PRESSURE_BUCKET, schema.SYSTEM_PRESSURE,
        schema.SYSTEM_PRESSURE_PSI, schema.SYSTEM_PRESSURE_UNITS,
        schema.MAIN_COUNT, schema.LENGTH_FT, schema.SOURCE_IDS,
    ]
    frame = pd.DataFrame({name: [] for name in columns})
    frame["geometry"] = []
    return gpd.GeoDataFrame(frame, geometry="geometry", crs=crs)


def multipart(geometry):
    """A geometry as a MultiLineString, for writing to a GeoPackage layer.

    A GeoPackage layer holds one geometry type. A dissolve produces LineStrings
    for simple runs and MultiLineStrings for branching ones, and writing the
    mixture makes the layer's declared type depend on which system happened to
    be first.
    """
    if geometry is None or geometry.is_empty:
        return None
    if isinstance(geometry, MultiLineString):
        return geometry
    if geometry.geom_type == "LineString":
        return MultiLineString([geometry])
    if geometry.geom_type == "GeometryCollection":
        lines = [part for part in geometry.geoms if part.geom_type == "LineString"]
        return MultiLineString(lines) if lines else None
    return None
