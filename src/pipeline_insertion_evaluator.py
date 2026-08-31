"""LPP GSEP pipeline insertion candidate identification.

Identifies GSEP-eligible Low Pressure Pipe systems that are candidates for
insertion into a nearby elevated pressure system. A candidate must be GSEP
eligible, part of a Lower Pressure distribution system, within 50 feet of an
Other Pressure system, and that system must be at or above the candidate's
pressure.

The run is one pass over MA Pressure View's Main Lines layer:

    download     layer 145, cached locally, delta-refreshed
    classify     GSEP eligibility, pressure bucket, pressure in PSI
    dissolve     contiguous connected mains at the same pressure -> systems
    near         each Lower Pressure system's nearest Other Pressure system
    select       distance <= 50 ft and target pressure >= candidate pressure
    write        the GeoPackage layers in the README's layer inventory

Nothing here decides anything. Every threshold is in `pipelineinsertion.config`
and every rule is a pure function in `gsep`, `pressure`, `systems` or `nearest`,
which is what lets the rules be tested without a token, a network or a GIS.

    python run.py                    everything, then serve the map
    python run.py --no-view          stop after the GeoPackage
    python run.py --refresh          ignore the layer cache
"""
import os
import sys
import time
import traceback
from contextlib import contextmanager

# The `pipelineinsertion` package sits next to this file. Adding the script's
# own directory to sys.path lets it import cleanly whether it is run from the
# repo or copied elsewhere - provided the package folder travels with it.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from pipelineinsertion import (
    arcgis,
    classify,
    config,
    crs,
    domains,
    nearest,
    schema,
    systems,
)
from pipelineinsertion.auth import make_session
from pipelineinsertion.output import detail, fail, log, step, warn

_TIMINGS = []


@contextmanager
def timed(label):
    started = time.time()
    try:
        yield
    finally:
        _TIMINGS.append((label, time.time() - started))


def report_timings():
    if not config.TIMINGS or not _TIMINGS:
        return
    step("Stage timings")
    width = max(len(label) for label, _ in _TIMINGS)
    for label, seconds in _TIMINGS:
        log(f"  {label:<{width}}  {seconds:8.1f}s")
    log(f"  {'total':<{width}}  {sum(s for _, s in _TIMINGS):8.1f}s")


def load_main_lines(session):
    """Download Main Lines and resolve the field names this workflow reads."""
    gdf, meta = arcgis.query_layer(
        session, config.MAIN_LINES_URL, config.WHERE_MA, config.MAIN_LINES_LAYER_NAME)
    resolved = arcgis.resolve_fields(
        arcgis.metadata_field_names(meta), config.MAIN_LINES_LAYER_NAME)
    return gdf, meta, resolved


def build_systems(mains, resolved, bucket_name):
    """Dissolve one bucket's mains into systems."""
    step(f"Dissolving {bucket_name} mains into systems")
    if len(mains) == 0:
        warn(f"No {bucket_name} mains, so there are no {bucket_name} systems.")
    return systems.dissolve(
        mains,
        guid_field=resolved.get("globalid") or "",
        legacy_field=resolved.get("legacyid") or "",
    )


def drop_unusable_geometry(frame, layer_name):
    """Remove rows whose geometry cannot be written or read back.

    The last line of defence before the GeoPackage. A geometry with NaN
    coordinates writes without complaint and then poisons everything
    downstream: reading it back warns "invalid value encountered in from_wkb",
    the layer's `total_bounds` becomes NaN, and the map that frames itself on
    those bounds gets NaN and dies.

    Whatever produced such a row is a bug worth fixing at its source - this
    reports the count rather than quietly cleaning up after it.
    """
    usable = frame.geometry.notna() & ~frame.geometry.is_empty & frame.geometry.is_valid
    dropped = int((~usable).sum())
    if dropped:
        warn(f"{layer_name}: {dropped:,} of {len(frame):,} features have "
             f"unusable geometry and are not written. This is a defect "
             f"upstream of the write, not a property of the data.")
    return frame[usable].copy()


def write_outputs(layers):
    """Write every non-empty layer to the GeoPackage.

    Geometries are normalised to MultiLineString first: a GeoPackage layer holds
    one geometry type, and a dissolve produces a mix of LineString and
    MultiLineString depending on whether a system branches.

    An empty layer is created rather than skipped. A missing layer is
    indistinguishable from a workflow that failed part-way; an empty one with
    the right columns says clearly that the stage ran and found nothing.
    """
    step("Writing the GeoPackage")
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    target = config.OUTPUT_GPKG
    if target.exists():
        # Written fresh each run. Appending to an existing GeoPackage leaves the
        # previous run's candidates alongside this one's, which is how a stale
        # result gets reviewed as a current one.
        target.unlink()
        detail(f"Removed the previous GeoPackage at {target}")

    written = {}
    for name, gdf in layers.items():
        if gdf is None:
            continue
        frame = gdf.copy()
        if len(frame):
            frame["geometry"] = frame.geometry.apply(systems.multipart)
            frame = frame[frame.geometry.notna()].copy()
            frame = drop_unusable_geometry(frame, name)
        frame.to_file(target, layer=name, driver="GPKG")
        written[name] = len(frame)
        log(f"  {name}: {len(frame):,} features")

    log(f"GeoPackage: {target}")
    return written


def main():
    step("Starting the LPP GSEP pipeline insertion evaluator")
    for key, value in sorted(config.describe().items()):
        log(f"  {key}: {value}")

    session = make_session()

    with timed("download Main Lines"):
        mains, meta, resolved = load_main_lines(session)
    if len(mains) == 0:
        fail(f"{config.MAIN_LINES_LAYER_NAME} returned no features for WHERE "
             f"[{config.WHERE_MA}]. There is nothing to evaluate.")

    layer_json = meta.get("layer_json")
    domains.check_pressure_units(layer_json)

    with timed("choose the analysis CRS"):
        target_crs = crs.analysis_crs(mains.crs)
        mains = crs.to_analysis_crs(mains, target_crs, config.MAIN_LINES_LAYER_NAME)

    with timed("classify"):
        classified = classify.classify(
            mains, resolved, domains.material_labels(layer_json))
        lower_mains = classify.lower_pressure_candidates(classified)
        other_mains = classify.other_pressure_targets(classified)

    with timed("dissolve"):
        lower_systems = build_systems(lower_mains, resolved, config.BUCKET_LOWER)
        other_systems = build_systems(other_mains, resolved, config.BUCKET_OTHER)

    step("Finding the nearest Other Pressure system for each candidate")
    log(f"Candidate must be within {config.MAX_DISTANCE_FT:g} ft of a system at "
        f"or above its own pressure.")
    with timed("near analysis"):
        near_table, paths, candidates = nearest.analyse(lower_systems, other_systems)

    with timed("write outputs"):
        written = write_outputs({
            # Classified mains, before the dissolve.
            schema.GSEP_LOWER_PRESSURE_LAYER: lower_mains,
            schema.OTHER_PRESSURE_MAINS_LAYER: other_mains,
            # Dissolved systems. ElevatedPressureSystems and the Other Pressure
            # systems have identical definitions in the README, so they are the
            # same features written under both of the names it asks for rather
            # than two selections that could drift apart.
            schema.LOWER_PRESSURE_SYSTEMS_LAYER: lower_systems,
            schema.ELEVATED_PRESSURE_SYSTEMS_LAYER: other_systems,
            # The analysis.
            schema.INSERTION_PATHS_LAYER: paths,
            schema.NEAR_AUDIT_TABLE: near_table,
            schema.CANDIDATES_LAYER: candidates,
        })

    step("Finished")
    log(f"{written.get(schema.CANDIDATES_LAYER, 0):,} insertion candidates from "
        f"{len(lower_systems):,} Lower Pressure systems.")
    report_timings()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - the top-level handler catches everything
        # Its whole purpose: print the traceback and exit non-zero, rather than
        # let a bare stack trace scroll past in a console that then closes.
        print(traceback.format_exc(), flush=True)
        sys.exit(1)
