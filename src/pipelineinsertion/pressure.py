"""Pressure classification: which bucket a main is in, and in what unit.

Two buckets matter to this workflow.

    Lower Pressure  the insertion candidates
                    pressureunits = WC  and pressure <= 60" WC
                    pressureunits = PSI and pressure <= 2 PSI

    Other Pressure  the insertion targets, also published as the elevated
                    pressure systems
                    pressureunits = PSI and 2 PSI < pressure <= 124 PSI

The 60" WC threshold is wider than the 14" WC classification boundary on
purpose: it catches systems that were never re-recorded in PSI after roughly
0.5 PSI. It is `config.LOWER_PRESSURE_MAX_WC`, not a literal, so widening or
narrowing it is one edit.

Unknown units are excluded from both buckets. A pressure with no unit is a
number, not a pressure, and putting it in a bucket means guessing which one -
which for a value like 5 is the difference between a candidate and a target.

--- On comparing a candidate against its target ---

The README states the final test as:

    NEAREST_EP_PRESSURE >= SYSTEM_PRESSURE

Compared as raw numbers that test is wrong whenever the candidate is recorded
in water column, which is most of them: a 55" WC candidate is about 2.0 PSI, so
a 5 PSI target really does exceed it, but `5 >= 55` is false and the candidate
is dropped. Every value is therefore converted to PSI with `to_psi` before the
comparison - see `target_serves_candidate`. Both the raw values and their units
are still written to the output, so the conversion can be checked rather than
taken on trust.
"""
# Absolute imports with this path setup, rather than relative imports, so the
# module also works when loaded by file path or run directly - not only when
# imported as a package member.
import os as _os
import sys as _sys

_PACKAGE_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PACKAGE_PARENT not in _sys.path:
    _sys.path.insert(0, _PACKAGE_PARENT)

from pipelineinsertion import config
from pipelineinsertion.fields import parse_number

UNIT_LABELS = {
    config.PRESSURE_UNIT_UNKNOWN: "Unknown",
    config.PRESSURE_UNIT_PSI: "PSI",
    config.PRESSURE_UNIT_WC: "WC",
}

# Not in either bucket. Kept as a value rather than as "" so a frame can be
# grouped and counted by bucket without nulls needing special handling.
BUCKET_NONE = ""


def unit_code(value):
    """A pressureunits domain code as an int, or None.

    Codes arrive as ints from the service and as floats once pandas has seen a
    null in the column, so 1.0 and 1 have to mean PSI.
    """
    number = parse_number(value)
    if number is None:
        return None
    return int(number)


def unit_label(value):
    """"PSI", "WC", "Unknown", or "" for a code outside the domain."""
    return UNIT_LABELS.get(unit_code(value), "")


def pressure_value(operating_pressure, maop_record=None):
    """The pressure to classify on: OPERATINGPRESSURE, else MAOPRECORD.

    This is the README's COALESCE. A main with a null operating pressure still
    has a recorded MAOP, and dropping those loses real systems - but the
    fallback is only used when the first value is genuinely absent, not when it
    is zero, which is a pressure like any other.
    """
    value = parse_number(operating_pressure)
    if value is not None:
        return value
    return parse_number(maop_record)


def to_psi(value, units):
    """A pressure in PSI, whatever unit it was recorded in. None if unusable.

    Unknown units convert to None rather than being assumed to be PSI: an
    assumed unit is how a 55 that meant water column becomes a 55 PSI system.
    """
    number = parse_number(value)
    if number is None:
        return None
    code = unit_code(units)
    if code == config.PRESSURE_UNIT_PSI:
        return number
    if code == config.PRESSURE_UNIT_WC:
        return number / config.WC_PER_PSI
    return None


def is_lower_pressure(value, units):
    """The Lower Pressure bucket test - the insertion candidates."""
    number = parse_number(value)
    if number is None:
        return False
    code = unit_code(units)
    if code == config.PRESSURE_UNIT_WC:
        return number <= config.LOWER_PRESSURE_MAX_WC
    if code == config.PRESSURE_UNIT_PSI:
        return number <= config.LOWER_PRESSURE_MAX_PSI
    return False


def is_other_pressure(value, units):
    """The Other Pressure bucket test - the insertion targets.

    PSI only. A water-column value cannot exceed 2 PSI and still be inside the
    Lower Pressure catch-all, and a system recorded in WC at a genuinely
    elevated pressure is a data problem to be fixed at source rather than
    reinterpreted here.
    """
    number = parse_number(value)
    if number is None:
        return False
    if unit_code(units) != config.PRESSURE_UNIT_PSI:
        return False
    return config.OTHER_PRESSURE_MIN_PSI < number <= config.OTHER_PRESSURE_MAX_PSI


def bucket(value, units):
    """"Lower Pressure", "Other Pressure", or "" for neither.

    Checked in that order. The two tests cannot both pass - Lower Pressure in
    PSI stops at 2 and Other Pressure starts above it - so the order documents
    intent rather than resolving an overlap.
    """
    if is_lower_pressure(value, units):
        return config.BUCKET_LOWER
    if is_other_pressure(value, units):
        return config.BUCKET_OTHER
    return BUCKET_NONE


def target_serves_candidate(candidate_psi, target_psi):
    """Whether a target system is at or above the candidate's pressure.

    Both arguments are already in PSI - convert with `to_psi` first. An unknown
    pressure on either side is not a match: an insertion whose target pressure
    nobody knows is not a reviewable candidate.
    """
    if candidate_psi is None or target_psi is None:
        return False
    return target_psi >= candidate_psi


def lower_pressure_where(units_field="pressureunits", pressure_field="OPERATINGPRESSURE"):
    """The Lower Pressure bucket as SQL, for a service-side query."""
    return (
        f"(({units_field} = {config.PRESSURE_UNIT_WC}"
        f" AND {pressure_field} <= {config.LOWER_PRESSURE_MAX_WC:g})"
        f" OR ({units_field} = {config.PRESSURE_UNIT_PSI}"
        f" AND {pressure_field} <= {config.LOWER_PRESSURE_MAX_PSI:g}))"
    )


def other_pressure_where(units_field="pressureunits", pressure_field="OPERATINGPRESSURE"):
    """The Other Pressure bucket as SQL, for a service-side query."""
    return (
        f"({units_field} = {config.PRESSURE_UNIT_PSI}"
        f" AND {pressure_field} > {config.OTHER_PRESSURE_MIN_PSI:g}"
        f" AND {pressure_field} <= {config.OTHER_PRESSURE_MAX_PSI:g})"
    )
