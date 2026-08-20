"""Choosing a coordinate system the distances can be measured in.

This workflow's whole answer is a distance threshold - 50 feet - so unlike a
workflow that only draws things, it cannot use whatever CRS the layer happened
to arrive in. Two failure modes matter:

* A geographic CRS (WGS 84, NAD83) measures in degrees. `geometry.distance`
  returns a number without complaint, and at Massachusetts' latitude 50 feet is
  about 0.00014 degrees - so a threshold of 50 silently accepts everything.
* A metre-based projected CRS measures in metres. The comparison still runs and
  every distance is understated by a factor of 3.28, so a 50 ft filter quietly
  becomes a 164 ft one.

Neither raises. Both change the deliverable. So the analysis CRS is chosen
explicitly here: the layer's own spatial reference when its linear unit is
already feet, and `config.FALLBACK_ANALYSIS_EPSG` otherwise.
"""
# Absolute imports with this path setup, rather than relative imports, so the
# module also works when loaded by file path or run directly - not only when
# imported as a package member.
import os as _os
import sys as _sys

_PACKAGE_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PACKAGE_PARENT not in _sys.path:
    _sys.path.insert(0, _PACKAGE_PARENT)

from pyproj import CRS

from pipelineinsertion import config
from pipelineinsertion.output import log, warn

# What pyproj calls a foot, in any of the spellings an authority may use.
FOOT_UNIT_NAMES = {
    "us survey foot", "foot_us", "usft", "us_survey_foot",
    "foot", "ft", "international foot", "british foot",
}


def is_foot_based(crs):
    """True when one unit of `crs` is a foot.

    Checked on the axis unit rather than on a list of known EPSG codes, so a
    State Plane zone this project has never seen is still recognised.
    """
    if crs is None:
        return False
    try:
        crs = CRS.from_user_input(crs)
    except Exception:  # noqa: BLE001 - pyproj raises CRSError for anything unusable
        return False
    if not crs.is_projected:
        return False
    try:
        units = {axis.unit_name.strip().lower() for axis in crs.axis_info}
    except AttributeError:
        return False
    return bool(units) and units.issubset(FOOT_UNIT_NAMES)


def analysis_crs(source_crs):
    """The CRS to run the distance analysis in.

    Returns the source CRS when it already measures in feet, so the geometries
    do not have to be reprojected at all - a reprojection of every main in the
    state is neither free nor lossless. Otherwise the configured fallback.
    """
    if is_foot_based(source_crs):
        log(f"Analysis CRS: {CRS.from_user_input(source_crs).to_string()} "
            f"(the layer's own, already in feet).")
        return CRS.from_user_input(source_crs)

    fallback = CRS.from_epsg(config.FALLBACK_ANALYSIS_EPSG)
    if source_crs is None:
        warn(f"The layer reported no spatial reference. Assuming its "
             f"coordinates are already {fallback.to_string()}; if they are "
             f"not, every distance in the output is wrong. Set "
             f"PIPEINSERT_ANALYSIS_EPSG if this is the wrong zone.")
    else:
        log(f"Analysis CRS: EPSG:{config.FALLBACK_ANALYSIS_EPSG} "
            f"({fallback.name}). The layer is in "
            f"{CRS.from_user_input(source_crs).to_string()}, which does not "
            f"measure in feet, so it is reprojected before any distance is "
            f"measured.")
    return fallback


def to_analysis_crs(gdf, target_crs, layer_name):
    """`gdf` in `target_crs`, reprojecting only when it is not already there."""
    if gdf is None or len(gdf) == 0:
        return gdf
    if gdf.crs is None:
        warn(f"{layer_name}: no CRS on the downloaded layer. Assuming "
             f"{CRS.from_user_input(target_crs).to_string()} and not "
             f"reprojecting.")
        return gdf.set_crs(target_crs, allow_override=True)
    if CRS.from_user_input(gdf.crs) == CRS.from_user_input(target_crs):
        return gdf
    log(f"{layer_name}: reprojecting {len(gdf):,} features to "
        f"{CRS.from_user_input(target_crs).to_string()}.")
    return gdf.to_crs(target_crs)
