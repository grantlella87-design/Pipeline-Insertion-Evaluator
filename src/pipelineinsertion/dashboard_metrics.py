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

from pipelineinsertion import config, insertability, nearest, schema
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


# Length bins for the candidate-length chart, in feet. A system is a run of
# contiguous mains, so the spread is wide and the interesting end is the short
# one - a 40 ft system and a 4,000 ft system are different pieces of work.
LENGTH_BINS = (0, 100, 250, 500, 1000, 2500, 5000, 10000)


def mean(values):
    """The arithmetic mean, or None for nothing to average."""
    usable = [value for value in values if value is not None]
    return (sum(usable) / len(usable)) if usable else None


def length_stats(frame, column=None):
    """Total, mean, median and range of a length column.

    Mean and median are both reported because they disagree here and the
    disagreement is the point: system lengths are heavily right-skewed - a few
    long runs pull the mean well above the median - so a single "average
    length" would misdescribe most of the list.
    """
    column = column or schema.LENGTH_FT
    lengths = _numbers(frame, column)
    return {
        "count": len(lengths),
        "total": sum(lengths),
        "mean": mean(lengths),
        "median": percentile(lengths, 0.5),
        "min": min(lengths) if lengths else None,
        "max": max(lengths) if lengths else None,
    }


def length_histogram(frame, bins=LENGTH_BINS, column=None):
    """[(label, count, total_ft)] over a length column.

    Each bin carries its footage as well as its count, because "how many
    systems" and "how much main" are different questions and the second is the
    one that sizes the work.
    """
    column = column or schema.LENGTH_FT
    lengths = _numbers(frame, column)
    rows = []
    for index, lower in enumerate(bins):
        upper = bins[index + 1] if index + 1 < len(bins) else None
        if upper is None:
            inside = [value for value in lengths if value >= lower]
            label = f"{lower:,.0f}+ ft"
        else:
            inside = [value for value in lengths if lower <= value < upper]
            label = f"{lower:,.0f}–{upper:,.0f} ft"
        rows.append((label, len(inside), sum(inside)))
    return rows


def geometry_length_ft(frame):
    """Total length of a frame's geometry, in feet, or None if it cannot be.

    The analysis CRS measures in US survey feet, so a sum of `.length` is
    already footage. Returning None rather than a number when the CRS is not
    foot-based is deliberate: a total in degrees or metres presented as feet is
    worse than no total, and nothing about the number itself would show it.
    """
    if frame is None or not len(frame) or "geometry" not in frame:
        return 0.0
    try:
        from pipelineinsertion import crs as crs_module

        if frame.crs is not None and not crs_module.is_foot_based(frame.crs):
            return None
        return float(frame.geometry.length.sum())
    except Exception:  # noqa: BLE001 - a length that cannot be measured is None
        return None


def gsep_length(layers):
    """Total GSEP-eligible main length, in feet, split by pressure bucket.

    Both written main layers carry GSEP_ELIGIBLE, so this is the whole
    GSEP-eligible population that reached a bucket. Mains in neither bucket -
    an unknown pressure unit, say - are in no layer and so are not counted;
    `classify` reports that count at run time.
    """
    lower = layers.get(schema.GSEP_LOWER_PRESSURE_LAYER)
    other = layers.get(schema.OTHER_PRESSURE_MAINS_LAYER)

    lower_length = geometry_length_ft(lower)
    other_eligible = None
    if other is not None and schema.GSEP_ELIGIBLE in getattr(other, "columns", []):
        other_eligible = geometry_length_ft(other[other[schema.GSEP_ELIGIBLE] == 1])

    total = None
    if lower_length is not None and other_eligible is not None:
        total = lower_length + other_eligible
    elif lower_length is not None and other is None:
        total = lower_length

    return {
        # Every main in the GSEP_LPP_LowerPressure layer is GSEP eligible by
        # construction, so its whole length counts.
        "lower_pressure": lower_length,
        "other_pressure": other_eligible,
        "total": total,
    }


def distance_scan_rows(near):
    """[(distance_ft, length_ft, pressure_ok)] for the what-if control.

    One row per Lower Pressure system that has a nearest target, which is what
    lets the page answer "how much main becomes insertable at N ft" without
    re-running anything. `pressure_ok` is carried separately because distance
    is the constraint worth relaxing and pressure is not - a target below the
    candidate's pressure does not become usable by moving a threshold.

    Rounded, because the page embeds these and the precision is not the point.
    """
    if near is None or not len(near):
        return []

    distances = _values(near, schema.DISTANCE_FT)
    lengths = _values(near, schema.LENGTH_FT)
    system_psi = _values(near, schema.SYSTEM_PRESSURE_PSI)
    target_psi = _values(near, schema.NEAREST_EP_PRESSURE_PSI)
    if not (len(distances) == len(lengths) == len(system_psi) == len(target_psi)):
        return []

    rows = []
    for distance, length, mine, theirs in zip(distances, lengths,
                                              system_psi, target_psi):
        d = parse_number(distance)
        if d is None or not math.isfinite(d):
            continue  # no target at all: no distance would ever include it
        ok = nearest.candidate_status(d, parse_number(mine), parse_number(theirs),
                                      max_distance_ft=float("inf"))[0]
        rows.append((round(d, 1), round(parse_number(length) or 0.0, 1), bool(ok)))
    return rows


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

    # Mains admitted at exactly the minimum bore because of their pressure. The
    # carve-out is narrow enough that its size is worth stating rather than
    # leaving the reader to assume it is zero or assume it is most of them.
    at_minimum = sum(
        1 for value in _values(lower_mains, schema.INSERTION_REASON)
        if clean(value) == insertability.REASON_INSERTABLE_AT_ELEVATED)

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
            ("GSEP-eligible, insertable Lower Pressure mains",
             _count(lower_mains)),
            ("Other Pressure mains (targets)", _count(other_mains)),
            ("Lower Pressure systems", _count(lower_systems)),
            ("Other Pressure systems", _count(other_systems)),
            ("Insertion candidates", candidate_count),
        ],

        "at_minimum_mains": at_minimum,

        # --- panels ---
        "status": status_counts(near),
        "distances": distance_histogram(near),
        "materials": split_counts(candidates, schema.MATERIALS),
        "decades": decade_counts(candidates),
        "subnetworks": subnetwork_counts(candidates),
        "headroom_median": percentile(headroom, 0.5),
        "length_stats": length_stats(candidates),
        "length_histogram": length_histogram(candidates),
        "gsep_length": gsep_length(layers),
        "distance_scan": distance_scan_rows(near),
        "headroom_min": min(headroom) if headroom else None,
        "crossing_systems": sum(
            1 for value in _numbers(candidates, schema.CP_SUBNETWORK_COUNT)
            if value > 1),

        # --- thresholds, so the page states the rules it was run under ---
        "max_distance_ft": config.MAX_DISTANCE_FT,
        "near_search_limit_ft": config.NEAR_SEARCH_LIMIT_FT,
        "lower_max_wc": config.LOWER_PRESSURE_MAX_WC,
        "lower_max_psi": config.LOWER_PRESSURE_MAX_PSI,
        "other_min_psi": config.OTHER_PRESSURE_MIN_PSI,
        "other_max_psi": config.OTHER_PRESSURE_MAX_PSI,
        "cast_iron_max_diameter": config.CAST_IRON_MAX_DIAMETER_IN,
        "min_insertion_diameter_in": config.MIN_INSERTION_DIAMETER_IN,
        "insertion_elevated_psi": config.INSERTION_ELEVATED_PRESSURE_PSI,
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
