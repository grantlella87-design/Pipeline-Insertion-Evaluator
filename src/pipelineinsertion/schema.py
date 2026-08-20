"""The layer and column names this project writes, stated once.

These are not guesses. Every name here is written by code in this repository,
so reading one back by candidate list would be sniffing for something already
known - and picking the wrong column silently when the guess missed. Field
names that come from *outside* the project are a different problem and are not
listed here: the Main Lines field names are an external schema, and the
workflow resolves those through `fields.resolve_field_name`.

The layer names match the README's layer inventory, because those names are
what an engineer opening the GeoPackage will look for.
"""

# --- Output layers -----------------------------------------------------------

# Classified mains, before any dissolve. Kept because a dissolved system is hard
# to argue with on its own: this is where a system's constituent mains are.
GSEP_LOWER_PRESSURE_LAYER = "GSEP_LPP_LowerPressure"
OTHER_PRESSURE_MAINS_LAYER = "OtherPressureSystems"

# Dissolved systems.
LOWER_PRESSURE_SYSTEMS_LAYER = "LPP_LowerPressure_Systems"
ELEVATED_PRESSURE_SYSTEMS_LAYER = "ElevatedPressureSystems"

# The near analysis, as lines from each candidate to its nearest target.
INSERTION_PATHS_LAYER = "LPP_GSEP_InsertionPaths"

# The deliverable: the systems that passed both final tests.
CANDIDATES_LAYER = "LPP_GSEP_PipelineInsertionCandidates"

# Every Lower Pressure system with its near result, passed or not, and the
# reason. Not in the README's inventory; it is what makes a candidate count
# reviewable, because "excluded" and "never examined" look identical without it.
NEAR_AUDIT_TABLE = "LPP_GSEP_NearAudit"

# --- Classified main columns -------------------------------------------------

GSEP_ELIGIBLE = "GSEP_ELIGIBLE"
GSEP_REASON = "GSEP_REASON"
MATERIAL = "MATERIAL"
PRESSURE_BUCKET = "PRESSURE_BUCKET"
# The pressure the classification was made on - OPERATINGPRESSURE, or MAOPRECORD
# where the first was null - kept alongside the unit it was recorded in.
PRESSURE = "PRESSURE"
PRESSURE_UNITS = "PRESSURE_UNITS"
PRESSURE_UNIT_LABEL = "PRESSURE_UNIT_LABEL"
# The same pressure in PSI. Written because it is what the final comparison
# actually uses, and a reviewer should not have to redo the conversion.
PRESSURE_PSI = "PRESSURE_PSI"
# True when PRESSURE fell back to MAOPRECORD.
PRESSURE_FROM_MAOP = "PRESSURE_FROM_MAOP"

# --- System columns ----------------------------------------------------------

SYSTEM_ID = "SYSTEM_ID"
SYSTEM_PRESSURE = "SYSTEM_PRESSURE"
SYSTEM_PRESSURE_PSI = "SYSTEM_PRESSURE_PSI"
SYSTEM_PRESSURE_UNITS = "SYSTEM_PRESSURE_UNITS"
# How many source mains dissolved into this system, and their total length.
MAIN_COUNT = "MAIN_COUNT"
LENGTH_FT = "LENGTH_FT"
# {GUID}|LegacyID;{GUID}|LegacyID - see systems.source_ids.
SOURCE_IDS = "SOURCE_IDS"

# --- Near analysis columns ---------------------------------------------------

NEAREST_EP_ID = "NEAREST_EP_ID"
NEAREST_EP_PRESSURE = "NEAREST_EP_PRESSURE"
NEAREST_EP_PRESSURE_PSI = "NEAREST_EP_PRESSURE_PSI"
NEAREST_EP_PRESSURE_UNITS = "NEAREST_EP_PRESSURE_UNITS"
DISTANCE_FT = "DISTANCE_FT"
NEAR_X = "NEAR_X"
NEAR_Y = "NEAR_Y"
# The candidate-side end of the connection path, so the path can be rebuilt
# from the table alone.
FROM_X = "FROM_X"
FROM_Y = "FROM_Y"

# Whether the system passed the final test, and why not where it did not.
IS_CANDIDATE = "IS_CANDIDATE"
CANDIDATE_STATUS = "CANDIDATE_STATUS"

# The README's output field list for the near analysis, in its order. Written in
# this order so a table opened in ArcGIS reads the way the specification does.
NEAR_OUTPUT_FIELDS = (
    SYSTEM_ID,
    PRESSURE_BUCKET,
    SYSTEM_PRESSURE,
    NEAREST_EP_ID,
    NEAREST_EP_PRESSURE,
    DISTANCE_FT,
    NEAR_X,
    NEAR_Y,
    SOURCE_IDS,
)

# The README's suggested field list for the connection path layer.
INSERTION_PATH_FIELDS = (
    SYSTEM_ID,
    NEAREST_EP_ID,
    DISTANCE_FT,
    SYSTEM_PRESSURE,
    NEAREST_EP_PRESSURE,
    PRESSURE_BUCKET,
    SOURCE_IDS,
)


def require(frame, columns, what):
    """Raise unless every column is present, naming what is missing.

    Used instead of falling back to a different column, which is how a wrong
    guess becomes a wrong answer nobody notices.
    """
    missing = [name for name in columns if name not in frame.columns]
    if missing:
        raise KeyError(
            f"{what} is missing {missing}. Present: {sorted(frame.columns)}"
        )
