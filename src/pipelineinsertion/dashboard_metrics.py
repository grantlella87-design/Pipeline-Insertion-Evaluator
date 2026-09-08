"""The numbers behind the dashboard, computed apart from how they are drawn.

Separate from `dashboard.py` on purpose: a metric is worth testing and a `<svg>`
element is not. Everything here takes frames and returns plain Python, so the
whole dashboard can be checked without rendering anything.

Every count is derived from the GeoPackage the workflow wrote rather than
recomputed from the source layer. A dashboard that re-ran the analysis its own
way could disagree with the deliverable it claims to describe, and then there
would be two answers and no way to tell which one shipped.
"""
# Absolute imports with this path setup, rather than relative imports, so the
# module also works when loaded by file path or run directly - not only when
# imported as a package member.
import os as _os
import sys as _sys

_PACKAGE_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PACKAGE_PARENT not in _sys.path:
    _sys.path.insert(0, _PACKAGE_PARENT)

import math

from pipelineinsertion import config, nearest, schema
from pipelineinsertion.fields import clean, parse_number

FEET_PER_MILE = 5280.0

# Distance bins for the histogram, in feet. Deliberately finer below the
# threshold than above it: the question the chart answers is "how binding is
# 50 ft", and everything past a few hundred feet is one undifferentiated
# "nowhere near".
DISTANCE_BINS = (0, 10, 25, 50, 100, 250, 500, 1000, 2500)

# How a status reads to someone looking at the chart. The wording in the data is
# precise and long; these are the same statuses said briefly.
STATUS_LABELS = {
    nearest.STATUS_CANDIDATE: "Candidate",
    nearest.STATUS_TOO_FAR: "Nearest system too far",
    nearest.STATUS_TARGET_PRESSURE_TOO_LOW: "Target pressure below candidate",
    nearest.STATUS_NO_TARGET_IN_RANGE: "Nothing within search limit",
    nearest.STATUS_PRESSURE_NOT_COMPARABLE: "Pressure not comparable",
}


def _values(frame, column):
    """A column as a list, or [] when the frame has no such column.

    A GeoPackage written before the attribute columns existed is still worth a
    dashboard - it simply has fewer panels.
    """
    if frame is None or column not in getattr(frame, "columns", []):
        return []
    return list(frame[column])


def _numbers(frame, column):
    return [value for value in (parse_number(v) for v in _values(frame, column))
            if value is not None and math.isfinite(value)]


def _count(frame):
    return 0 if frame is None else int(len(frame))


def split_counts(frame, column):
    """Count the members of a semicolon-separated set column.

    MATERIALS on a dissolved system is "Cast Iron;Copper" - the set of what went
    into it. Counting the strings whole would report that pair as its own
    category, which is how a dashboard ends up with forty materials.
    """
    counts = {}
    for value in _values(frame, column):
        for part in str(clean(value)).split(";"):
            part = part.strip()
            if part:
                counts[part] = counts.get(part, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: -item[1]))


def status_counts(near):
    """{label: count} over every Lower Pressure system examined, biggest first."""
    counts = {}
    for value in _values(near, schema.CANDIDATE_STATUS):
        text = clean(value)
        label = STATUS_LABELS.get(text, text or "unrecorded")
        counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: -item[1]))


def distance_histogram(near, bins=DISTANCE_BINS):
    """[(label, count, upper_bound)] over the nearest-target distances.

    Systems with no target at all are not in here: they have no distance, and
    putting them in a "very far" bin would invent one. Their count is reported
    on its own - see `metrics["no_target"]`.
    """
    distances = _numbers(near, schema.DISTANCE_FT)
    rows = []
    for index, lower in enumerate(bins):
        upper = bins[index + 1] if index + 1 < len(bins) else None
        if upper is None:
            count = sum(1 for value in distances if value >= lower)
            label = f"{lower:,.0f}+ ft"
        else:
            count = sum(1 for value in distances if lower <= value < upper)
            label = f"{lower:,.0f}–{upper:,.0f} ft"
        rows.append((label, count, upper))
    return rows


def decade_counts(frame, column=None):
    """{decade label: count} from an ISO date column, oldest first.

    The age of a candidate system is what makes it a replacement priority, so
    this is the panel that connects the analysis to why GSEP exists at all.
    """
    column = column or schema.EARLIEST_INSTALL
    counts = {}
    for value in _values(frame, column):
        text = clean(value)
        if len(text) < 4 or not text[:4].isdigit():
            continue
        decade = (int(text[:4]) // 10) * 10
        label = f"{decade}s"
        counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items()))


def pressure_headroom(candidates):
    """How much spare pressure each candidate's target has, in PSI.

    Target minus candidate, both already normalised to PSI. A candidate whose
    target only just clears it is a different engineering proposition from one
    with 40 PSI in hand, and the raw recorded numbers do not show that - they
    are in different units.
    """
    system = _numbers(candidates, schema.SYSTEM_PRESSURE_PSI)
    target = _numbers(candidates, schema.NEAREST_EP_PRESSURE_PSI)
    if len(system) != len(target):
        return []
    return [round(t - s, 3) for s, t in zip(system, target)]


def subnetwork_counts(frame):
    """{"1 subnetwork": n, ...} - how many CP subnetworks each system spans.

    A system crossing a cathodic protection boundary is a real constructability
    finding, and it is invisible in a candidate list that only reports a name.
    """
    counts = {}
    for value in _numbers(frame, schema.CP_SUBNETWORK_COUNT):
        key = int(value)
        counts[key] = counts.get(key, 0) + 1
    labels = {}
    for key in sorted(counts):
        label = "1 subnetwork" if key == 1 else f"{key} subnetworks"
        if key == 0:
            label = "not recorded"
        labels[label] = counts[key]
    return labels


def percentile(values, fraction):
    """A percentile by nearest rank. Returns None for no values."""
    ordered = sorted(values)
    if not ordered:
        return None
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def collect(layers):
    """Every number the dashboard shows, from the written GeoPackage layers.

    `layers` is {layer name: GeoDataFrame or None}. A missing layer costs its
    panels and nothing else, so a dashboard can still be built from a partial
    run - which is exactly when someone wants to look at one.
    """
    near = layers.get(schema.NEAR_AUDIT_TABLE)
    candidates = layers.get(schema.CANDIDATES_LAYER)
    lower_mains = layers.get(schema.GSEP_LOWER_PRESSURE_LAYER)
    other_mains = layers.get(schema.OTHER_PRESSURE_MAINS_LAYER)
    lower_systems = layers.get(schema.LOWER_PRESSURE_SYSTEMS_LAYER)
    other_systems = layers.get(schema.ELEVATED_PRESSURE_SYSTEMS_LAYER)

    examined = _count(near) or _count(lower_systems)
    candidate_count = _count(candidates)
    distances = _numbers(near, schema.DISTANCE_FT)
    candidate_lengths = _numbers(candidates, schema.LENGTH_FT)
    candidate_mains = _numbers(candidates, schema.MAIN_COUNT)
    headroom = pressure_headroom(candidates)

    no_target = sum(
        1 for value in _values(near, schema.CANDIDATE_STATUS)
        if clean(value) == nearest.STATUS_NO_TARGET_IN_RANGE)

    return {
        # --- headline ---
        "candidates": candidate_count,
        "examined": examined,
        "conversion": (candidate_count / examined) if examined else None,
        "candidate_length_ft": sum(candidate_lengths),
        "candidate_length_miles": sum(candidate_lengths) / FEET_PER_MILE,
        "candidate_mains": int(sum(candidate_mains)),
        "median_distance_ft": percentile(
            [d for d in distances if d <= config.MAX_DISTANCE_FT], 0.5),
        "no_target": no_target,

        # --- the funnel, in the order the workflow applies it ---
        "funnel": [
            ("GSEP-eligible Lower Pressure mains", _count(lower_mains)),
            ("Other Pressure mains (targets)", _count(other_mains)),
            ("Lower Pressure systems", _count(lower_systems)),
            ("Other Pressure systems", _count(other_systems)),
            ("Insertion candidates", candidate_count),
        ],

        # --- panels ---
        "status": status_counts(near),
        "distances": distance_histogram(near),
        "materials": split_counts(candidates, schema.MATERIALS),
        "decades": decade_counts(candidates),
        "subnetworks": subnetwork_counts(candidates),
        "headroom_median": percentile(headroom, 0.5),
        "headroom_min": min(headroom) if headroom else None,
        "crossing_systems": sum(
            1 for value in _numbers(candidates, schema.CP_SUBNETWORK_COUNT)
            if value > 1),

        # --- thresholds, so the page states the rules it was run under ---
        "max_distance_ft": config.MAX_DISTANCE_FT,
        "lower_max_wc": config.LOWER_PRESSURE_MAX_WC,
        "lower_max_psi": config.LOWER_PRESSURE_MAX_PSI,
        "other_min_psi": config.OTHER_PRESSURE_MIN_PSI,
        "other_max_psi": config.OTHER_PRESSURE_MAX_PSI,
        "cast_iron_max_diameter": config.CAST_IRON_MAX_DIAMETER_IN,
        "coated_steel_cutoff": config.COATED_STEEL_INSTALLED_BEFORE,
        "plastic_pending": not config.PLASTIC_ASSETTYPES,
    }


def candidate_table(candidates, limit=250):
    """The candidate rows the page tabulates, nearest first.

    Nearest first because a 6 ft tie-in and a 49 ft one are not the same job,
    and the short ones are what anyone opens the list to find.
    """
    columns = [
        (schema.SYSTEM_ID, "System"),
        (schema.DISTANCE_FT, "Distance ft"),
        (schema.MATERIALS, "Materials"),
        (schema.SYSTEM_PRESSURE, "Pressure"),
        (schema.SYSTEM_PRESSURE_UNITS, "Units"),
        (schema.NEAREST_EP_ID, "Target"),
        (schema.NEAREST_EP_PRESSURE, "Target PSI"),
        (schema.MAIN_COUNT, "Mains"),
        (schema.LENGTH_FT, "Length ft"),
        (schema.CP_SUBNETWORKS, "CP subnetwork"),
        (schema.SOURCE_IDS, "Source IDs"),
    ]
    if candidates is None or not len(candidates):
        return [label for _, label in columns], []

    present = [(name, label) for name, label in columns
               if name in candidates.columns]
    frame = candidates
    if schema.DISTANCE_FT in frame.columns:
        frame = frame.sort_values(schema.DISTANCE_FT, na_position="last")

    rows = []
    for _, record in frame.head(limit).iterrows():
        rows.append([_cell(record[name]) for name, _ in present])
    return [label for _, label in present], rows


def _cell(value):
    number = parse_number(value)
    if number is not None and not isinstance(value, str):
        if float(number).is_integer():
            return f"{int(number):,}"
        return f"{number:,.2f}"
    return clean(value)
