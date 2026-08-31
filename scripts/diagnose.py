"""Where did the data go? Reports the funnel from download to candidates.

    python scripts/diagnose.py

An empty map has many possible causes and they look identical from the browser:
no GeoPackage, a download that returned nothing, a filter that matched nothing,
a dissolve that produced nothing, or coordinates in the wrong place. This walks
the same stages the workflow does and prints the count after each, so the stage
that lost the features names itself.

Runs off the layer cache, so it needs no token and no network - and it does not
modify anything. Without a cache it reports what it can and says what to run.

    python scripts/diagnose.py --where     print the SQL for each stage
"""
import sys

from _bootstrap import config

from pipelineinsertion import classify, crs, gsep, nearest, pressure, schema, systems
from pipelineinsertion.arcgis import (
    layer_cache_paths,
    metadata_field_names,
    resolve_fields,
)
from pipelineinsertion.output import log


def rule(title):
    log(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


def read_cache():
    """The cached Main Lines layer, or None."""
    import pandas as pd

    data_path, _ = layer_cache_paths(config.MAIN_LINES_LAYER_NAME)
    if not data_path.is_file():
        return None, data_path
    try:
        return pd.read_pickle(data_path, compression="gzip"), data_path
    except Exception as ex:  # noqa: BLE001 - reported, not raised
        log(f"  the cache at {data_path} could not be read: {ex}")
        return None, data_path


def report_paths():
    rule("Where things are")
    for key, value in sorted(config.describe().items()):
        log(f"  {key:20} {value}")

    data_path, meta_path = layer_cache_paths(config.MAIN_LINES_LAYER_NAME)
    log(f"\n  layer cache          {data_path}")
    log(f"  {'exists' if data_path.is_file() else 'MISSING':20} "
        f"{f'{data_path.stat().st_size / 1e6:.1f} MB' if data_path.is_file() else ''}")

    gpkg = config.OUTPUT_GPKG
    log(f"\n  GeoPackage           {gpkg}")
    log(f"  {'exists' if gpkg.is_file() else 'MISSING':20} "
        f"{f'{gpkg.stat().st_size / 1e6:.1f} MB' if gpkg.is_file() else ''}")


def report_geopackage():
    rule("What is in the GeoPackage")
    gpkg = config.OUTPUT_GPKG
    if not gpkg.is_file():
        log(f"  There is no GeoPackage at {gpkg}.")
        log("  The workflow has not completed. Run: python run.py --no-view")
        return

    import geopandas as gpd

    try:
        names = list(gpd.list_layers(gpkg)["name"])
    except Exception as ex:  # noqa: BLE001 - reported, not raised
        log(f"  Could not list its layers: {ex}")
        return

    empty = []
    for name in names:
        frame = gpd.read_file(gpkg, layer=name)
        marker = "" if len(frame) else "   <- EMPTY"
        log(f"  {len(frame):>9,}  {name}{marker}")
        if not len(frame):
            empty.append(name)
        elif name == schema.CANDIDATES_LAYER:
            report_extent(frame, "    candidates")

    if empty and len(empty) == len(names):
        log("\n  Every layer is empty, so the analysis found nothing. The stage "
            "that lost the features is shown below.")


def report_extent(frame, label):
    """Where a layer actually is on the earth. The check that catches a bad CRS."""
    import geopandas as gpd

    if frame.crs is None:
        log(f"{label}: NO CRS - positions cannot be trusted")
        return
    try:
        wgs84 = frame.to_crs(epsg=4326)
    except Exception as ex:  # noqa: BLE001 - reported, not raised
        log(f"{label}: could not reproject to lat/lon: {ex}")
        return
    west, south, east, north = wgs84.total_bounds
    log(f"{label}: {crs.describe(frame.crs)}")
    log(f"{label}: lat {south:.4f}..{north:.4f}  lon {west:.4f}..{east:.4f}")

    # Massachusetts, generously. A layer outside this is in the wrong place,
    # which is what a misread spatial reference looks like from here.
    if not (40.5 <= south <= 43.5 and -74.5 <= west <= -69.0):
        log(f"{label}: *** NOT IN MASSACHUSETTS ***")
        log(f"{label}: the layer's projection is being read wrongly, so every "
            f"position is off. Re-download with: python run.py --refresh")


def report_funnel(mains, show_where):
    rule("The funnel, stage by stage")

    log(f"  {len(mains):>9,}  mains in the cache")
    report_extent(mains, "            ")

    resolved = resolve_fields(list(mains.columns), config.MAIN_LINES_LAYER_NAME)
    log("\n  Field names resolved against the cache:")
    for purpose, name in sorted(resolved.items()):
        mark = " " if name else "!"
        log(f"   {mark} {purpose:16} -> {name}")

    log("\n  How populated the fields that decide the answer are:")
    for purpose in ("assettype", "pressure", "pressure_units", "diameter",
                    "installed", "maop", "globalid", "legacyid"):
        name = resolved.get(purpose)
        if not name or name not in mains.columns:
            log(f"    {purpose:16} (absent)")
            continue
        filled = int(mains[name].notna().sum())
        share = 100.0 * filled / max(len(mains), 1)
        log(f"    {purpose:16} {filled:>9,} of {len(mains):,} ({share:5.1f}%) populated")

    classified = classify.classify(mains, resolved)

    log("\n  GSEP eligibility:")
    for reason, count in classified[schema.GSEP_REASON].value_counts().items():
        verdict = "eligible" if reason in gsep.ELIGIBLE_REASONS else "excluded"
        log(f"    {count:>9,}  {verdict:9} {reason}")

    log("\n  Pressure buckets (all mains, before GSEP):")
    for bucket, count in classified[schema.PRESSURE_BUCKET].value_counts().items():
        log(f"    {count:>9,}  {bucket or '(neither bucket)'}")

    log("\n  Pressure units as recorded:")
    for code, count in classified[schema.PRESSURE_UNITS].value_counts(
            dropna=False).items():
        log(f"    {count:>9,}  {code} = {pressure.unit_label(code) or 'not in the domain'}")

    lower_mains = classify.lower_pressure_candidates(classified)
    other_mains = classify.other_pressure_targets(classified)

    log(f"\n  {len(lower_mains):>9,}  bucket 1 mains (GSEP eligible AND Lower Pressure)")
    log(f"  {len(other_mains):>9,}  bucket 2 mains (Other Pressure, any material)")

    if not len(lower_mains):
        log("\n  Nothing reaches bucket 1, so there can be no candidates. The "
            "eligibility and bucket tables above say which test excluded them.")
        return
    if not len(other_mains):
        log("\n  There are no Other Pressure systems to insert into, so no "
            "candidate can qualify. Check the pressure-units table above.")
        return

    target_crs = crs.analysis_crs(mains.crs)
    lower_mains = crs.to_analysis_crs(lower_mains, target_crs, "bucket 1")
    other_mains = crs.to_analysis_crs(other_mains, target_crs, "bucket 2")

    lower_systems = systems.dissolve(lower_mains, resolved.get("globalid") or "",
                                     resolved.get("legacyid") or "")
    other_systems = systems.dissolve(other_mains, resolved.get("globalid") or "",
                                     resolved.get("legacyid") or "")
    log(f"\n  {len(lower_systems):>9,}  Lower Pressure systems (dissolved)")
    log(f"  {len(other_systems):>9,}  Other Pressure systems (dissolved)")

    near, paths, candidates = nearest.analyse(lower_systems, other_systems)
    log(f"\n  {len(candidates):>9,}  CANDIDATES")

    if len(near):
        log("\n  Nearest-target distances, to show whether 50 ft is the binding "
            "constraint:")
        distances = near[schema.DISTANCE_FT].dropna()
        if len(distances):
            for label, value in (("minimum", distances.min()),
                                 ("median", distances.median()),
                                 ("90th percentile", distances.quantile(0.9))):
                log(f"    {label:16} {value:>12,.1f} ft")
            within = int((distances <= config.MAX_DISTANCE_FT).sum())
            log(f"    within {config.MAX_DISTANCE_FT:g} ft   {within:>12,}")
        else:
            log("    no system had a target within the search limit at all")

    if show_where:
        rule("The equivalent SQL")
        log("GSEP eligibility:\n" + gsep.where_clause())
        log("\nLower Pressure:\n" + pressure.lower_pressure_where())
        log("\nOther Pressure:\n" + pressure.other_pressure_where())


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    report_paths()
    report_geopackage()

    mains, data_path = read_cache()
    if mains is None:
        rule("The funnel, stage by stage")
        log(f"  There is no readable layer cache at {data_path}, so the stages "
            f"below cannot be replayed offline.")
        log("  Download one with: python run.py --no-view")
        return 1
    if not len(mains):
        rule("The funnel, stage by stage")
        log("  The cache is empty: the download returned no features. Check the "
            "WHERE clause in config.WHERE_MA and re-run with --refresh.")
        return 1

    report_funnel(mains, show_where="--where" in argv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
