"""Single source of truth for every path, URL, threshold and tuning knob.

Every rule the README states as a number lives here rather than inline in the
analysis: the GSEP material codes, the pressure-bucket thresholds, the coated
steel cut-off date and the 50 ft proximity test. A threshold that is written
into a filter expression is a threshold nobody can change without reading the
filter first, and the ones in this project are the whole result.

Every value can be overridden with an environment variable, which is what makes
the workflow runnable off one person's workstation, and testable on a machine
with no GIS portal at all.

    PIPEINSERT_WORK_ROOT           local scratch/cache root
    PIPEINSERT_CACHE_DIR           layer cache (defaults under work root)
    PIPEINSERT_OUTPUT_GPKG         production GeoPackage
    PIPEINSERT_GIS_ROOT            ArcGIS server root
    PIPEINSERT_PORTAL_ROOT         ArcGIS portal root
    PIPEINSERT_MAX_DISTANCE_FT     candidate proximity threshold (default 50)
"""
import os
from pathlib import Path

# --- Locations ---------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

DEFAULT_WORK_ROOT = Path.home() / "Downloads" / "PipelineInsertionEvaluator"


def _path_from_env(name, default):
    value = os.environ.get(name, "").strip()
    return Path(value) if value else default


WORK_ROOT = _path_from_env("PIPEINSERT_WORK_ROOT", DEFAULT_WORK_ROOT)

LAYER_CACHE_DIR = _path_from_env("PIPEINSERT_CACHE_DIR", WORK_ROOT / "layer_cache")
OUTPUT_DIR = WORK_ROOT / "insertion_candidate_outputs"

# The GeoPackage is written locally, under the work root. Writing it to a network
# location makes every run depend on network write throughput, and a partial
# write leaves the shared copy broken. Point PIPEINSERT_OUTPUT_GPKG somewhere
# else to publish deliberately.
OUTPUT_GPKG = _path_from_env(
    "PIPEINSERT_OUTPUT_GPKG", OUTPUT_DIR / "LPP_GSEP_PipelineInsertion.gpkg")

INPUT_DIR = REPO_ROOT / "input"

# Committed point-in-time copies of MapServer layer metadata. They carry the
# coded-value domains - ASSETTYPE and the pressure-units domain - which is what
# makes a decode possible without a token. Populate with:
#
#     python scripts/describe_layer.py <layer url> --save
REFERENCE_DIR = (REPO_ROOT / "reference" / "mapserver_json"
                 / "MA_Pressure_View_MA")

# Leaflet, committed so the map works with no internet and nothing to build
# first. Without a copy in the repo the page falls back to a CDN, and a network
# that blocks unpkg.com gives a blank map.
VENDORED_LEAFLET_DIR = REPO_ROOT / "vendor" / "leaflet"

# --- Services ----------------------------------------------------------------

GIS_ROOT = os.environ.get("PIPEINSERT_GIS_ROOT", "https://gis.nationalgrid.com").rstrip("/")
PORTAL_ROOT = os.environ.get("PIPEINSERT_PORTAL_ROOT", GIS_ROOT + "/portal").rstrip("/")

# MA Pressure View. Material View is not required: Main Lines carries the
# material identification (ASSETTYPE) as well as the pressure attributes, so one
# layer answers the whole question.
_MAP_SERVER = os.environ.get(
    "PIPEINSERT_MAP_SERVER",
    GIS_ROOT + "/arcgis/rest/services/MA/Pressure_View_MA/MapServer").rstrip("/")


def _int_from_env(name, default):
    try:
        return int(os.environ.get(name, "").strip())
    except (TypeError, ValueError):
        return default


def _float_from_env(name, default):
    try:
        return float(os.environ.get(name, "").strip())
    except (TypeError, ValueError):
        return default


def _flag_from_env(name, default=False):
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


MAIN_LINES_LAYER_ID = _int_from_env("PIPEINSERT_MAIN_LINES_LAYER_ID", 145)
MAIN_LINES_URL = os.environ.get(
    "PIPEINSERT_MAIN_LINES_URL", f"{_MAP_SERVER}/{MAIN_LINES_LAYER_ID}")

MAIN_LINES_LAYER_NAME = "main_lines"

PORTAL_AUTHORIZE_URL = PORTAL_ROOT + "/sharing/rest/oauth2/authorize"
PORTAL_TOKEN_URL = PORTAL_ROOT + "/sharing/rest/oauth2/token"

# --- Credentials -------------------------------------------------------------

ARCGIS_CLIENT_ID = os.environ.get("PIPEINSERT_CLIENT_ID", "48XCGWtLoUxA3klq")
ARCGIS_REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"
KEYRING_SERVICE = "NG_GIS_PIPELINE_INSERTION"
KEYRING_ACCESS_TOKEN_USER = "arcgis_portal_access_token"
KEYRING_ACCESS_TOKEN_EXPIRES_USER = "arcgis_portal_access_token_expires_epoch"
TOKEN_EXPIRY_SAFETY_SECONDS = 300

# --- GSEP eligibility --------------------------------------------------------

# ASSETTYPE coded values on Main Lines. These are the codes the production query
# uses; the decoded labels are read from the layer's own subtype domains, so a
# code that has been renamed on the service still reports its current label.
ASSETTYPE_BARE_STEEL = 1
ASSETTYPE_CAST_IRON = 2
ASSETTYPE_COATED_STEEL = 3
ASSETTYPE_COPPER = 5
ASSETTYPE_WROUGHT_IRON = 12

# Cast iron is eligible only up to this nominal diameter, in inches.
CAST_IRON_MAX_DIAMETER_IN = _float_from_env("PIPEINSERT_CAST_IRON_MAX_DIAMETER", 14.0)

# Coated steel is eligible only if installed before this date. Stored as a plain
# ISO string so it can go into a SQL literal and be parsed for the local check
# without a timezone entering the comparison.
COATED_STEEL_INSTALLED_BEFORE = os.environ.get(
    "PIPEINSERT_COATED_STEEL_CUTOFF", "1971-08-01")

# Plastic is deliberately absent. The README leaves plastic eligibility open
# until the GSEP program's plastic ASSETTYPE values are confirmed, so no plastic
# code is claimed here rather than a guess being made that quietly changes the
# candidate list. Add the confirmed codes to PLASTIC_ASSETTYPES to turn it on.
PLASTIC_ASSETTYPES = ()

# --- Pressure classification -------------------------------------------------

# 7_UPDM_UnitsForPressure domain.
PRESSURE_UNIT_UNKNOWN = 0
PRESSURE_UNIT_PSI = 1
PRESSURE_UNIT_WC = 2

# Lower Pressure, per unit.
LOWER_PRESSURE_MAX_PSI = _float_from_env("PIPEINSERT_LOWER_MAX_PSI", 2.0)
# Intentionally 60 rather than 14, to catch systems that never transitioned to
# PSI after roughly 0.5 PSI. 14" WC is the classification boundary; 60" WC is
# the catch-all the bucket query uses.
LOWER_PRESSURE_MAX_WC = _float_from_env("PIPEINSERT_LOWER_MAX_WC", 60.0)

# Other Pressure - the insertion targets. Always PSI: a WC-valued system is not
# an Other Pressure system whatever the number says.
OTHER_PRESSURE_MIN_PSI = _float_from_env("PIPEINSERT_OTHER_MIN_PSI", 2.0)
OTHER_PRESSURE_MAX_PSI = _float_from_env("PIPEINSERT_OTHER_MAX_PSI", 124.0)

# Inches of water column per PSI, at 60 degF. Used to put a WC-valued candidate
# and a PSI-valued target into the same unit before they are compared - see
# pressure.to_psi.
WC_PER_PSI = 27.7076

BUCKET_LOWER = "Lower Pressure"
BUCKET_OTHER = "Other Pressure"

# --- Proximity analysis ------------------------------------------------------

# A candidate must be within this distance of its nearest Other Pressure system.
MAX_DISTANCE_FT = _float_from_env("PIPEINSERT_MAX_DISTANCE_FT", 50.0)

# How far the near search looks before giving up on a Lower Pressure system.
# Systems with no Other Pressure system inside this radius are still reported,
# with no nearest target, so the count of "nothing near" is visible rather than
# being indistinguishable from a system that was never examined.
NEAR_SEARCH_LIMIT_FT = _float_from_env("PIPEINSERT_NEAR_SEARCH_LIMIT_FT", 5280.0)

# Two mains are treated as connected when their ends are within this distance.
# Exact coordinate equality misses mains that were digitised to within a
# hundredth of a foot of each other, which splits one physical system into two.
CONNECT_TOLERANCE_FT = _float_from_env("PIPEINSERT_CONNECT_TOLERANCE_FT", 0.1)

# Distances are in feet, so the analysis needs a foot-based projected CRS. The
# layer's own spatial reference is used when its linear unit is already feet;
# this is the fallback. 2249 is NAD83 / Massachusetts Mainland (ftUS).
FALLBACK_ANALYSIS_EPSG = _int_from_env("PIPEINSERT_ANALYSIS_EPSG", 2249)

# --- Matching / request tuning ------------------------------------------------

# Every main in Massachusetts. The GSEP and pressure filters are applied locally
# rather than pushed into the WHERE, so one download serves every bucket and a
# changed threshold does not mean a re-download.
WHERE_MA = os.environ.get("PIPEINSERT_WHERE", "1=1")

REQUEST_PAGE_SIZE = _int_from_env("PIPEINSERT_PAGE_SIZE", 2000)
REQUEST_TIMEOUT_SECONDS = _int_from_env("PIPEINSERT_TIMEOUT", 120)
OBJECTID_BATCH_SIZE = _int_from_env("PIPEINSERT_BATCH_SIZE", 2000)
OBJECTID_DOWNLOAD_WORKERS = _int_from_env("PIPEINSERT_DOWNLOAD_WORKERS", 8)
USE_OBJECTID_BATCH_DOWNLOAD = _flag_from_env("PIPEINSERT_OBJECTID_DOWNLOAD", True)
VERIFY_SSL = _flag_from_env("PIPEINSERT_VERIFY_SSL", True)

USE_LAYER_CACHE = _flag_from_env("USE_LAYER_CACHE", True)
FORCE_LAYER_REFRESH = _flag_from_env("FORCE_LAYER_REFRESH", False)

# A cache younger than this is trusted without asking the server whether it has
# changed. Each check costs a count query plus a delta query, and needs a valid
# token, so a short window makes repeat runs start immediately. Set to 0 to
# check the server every run.
CACHE_FRESH_SECONDS = _int_from_env("PIPEINSERT_CACHE_FRESH_SECONDS", 3600)

# --- Map ---------------------------------------------------------------------

# How far the map will zoom in. The tile providers stop at 19, so past that the
# deepest tile is upscaled while the systems stay sharp - they are vectors. The
# extra levels matter here: a 50 ft insertion path is a very short line.
MAP_MAX_ZOOM = _int_from_env("PIPEINSERT_MAP_MAX_ZOOM", 28)
TILE_MAX_NATIVE_ZOOM = _int_from_env("PIPEINSERT_TILE_MAX_NATIVE_ZOOM", 19)

# --- Output verbosity -------------------------------------------------------

# Field resolution, proxy/TLS setup and outFields lists are diagnostic detail.
# They are hidden unless something needs debugging.
VERBOSE = _flag_from_env("PIPEINSERT_VERBOSE", False)

# Print per-stage elapsed times, to show where a slow run spends its time.
TIMINGS = _flag_from_env("PIPEINSERT_TIMINGS", False)

# --- Authentication UX -----------------------------------------------------

# Capture the OAuth code on a loopback redirect instead of the out-of-band page.
# The browser lands on a page this process serves, which reports success and
# closes itself, so no code is shown and no tab is left behind.
#
# The portal app registration must list the redirect URI below. If it does not,
# authentication fails and the out-of-band flow is used instead.
USE_LOOPBACK_OAUTH = _flag_from_env("PIPEINSERT_LOOPBACK_OAUTH", True)
LOOPBACK_OAUTH_PORT = _int_from_env("PIPEINSERT_LOOPBACK_PORT", 8080)

# The portal compares redirect_uri against the app registration as a string, so
# host spelling and the trailing slash both matter: "localhost" and "127.0.0.1"
# are different values, and ".../" differs from "...". Set the whole URI to
# match the registration exactly when the defaults do not.
LOOPBACK_OAUTH_HOST = os.environ.get("PIPEINSERT_LOOPBACK_HOST", "localhost").strip()
LOOPBACK_REDIRECT_URI = (
    os.environ.get("PIPEINSERT_LOOPBACK_REDIRECT_URI", "").strip()
    or f"http://{LOOPBACK_OAUTH_HOST}:{LOOPBACK_OAUTH_PORT}/"
)


def describe():
    """Return the resolved configuration, for logging at startup."""
    return {
        "work_root": str(WORK_ROOT),
        "layer_cache_dir": str(LAYER_CACHE_DIR),
        "output_gpkg": str(OUTPUT_GPKG),
        "gis_root": GIS_ROOT,
        "main_lines_url": MAIN_LINES_URL,
        "max_distance_ft": MAX_DISTANCE_FT,
        "verify_ssl": VERIFY_SSL,
        "use_layer_cache": USE_LAYER_CACHE,
        "force_layer_refresh": FORCE_LAYER_REFRESH,
    }
