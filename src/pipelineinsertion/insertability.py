"""Whether a main is physically big enough to insert into.

Insertion threads a new plastic carrier pipe inside the existing main, so below
a certain bore there is no room for one. `config.MIN_INSERTION_DIAMETER_IN`
holds the threshold and the test is "greater than", so 4 inch itself is out -
with one carve-out.

A main at exactly the minimum is acceptable when it runs above
`config.INSERTION_ELEVATED_PRESSURE_PSI` (2 PSI). Pressure buys back the
capacity the narrower carrier pipe gives up, so the same load can be served
through a smaller bore. The carve-out is for the minimum itself only: a 3 inch
main stays out at any pressure.

The pressure tested is the main's own, in PSI - what it runs at today, not what
it would run at after being tied into an elevated system. Every candidate ends
up above 2 PSI once inserted, so testing the post-insertion pressure would admit
every 4 inch main and the rule would do no work.

Deliberately not part of `gsep.py`, and deliberately not folded into
GSEP_ELIGIBLE. The two answer different questions:

    GSEP eligible    is this main leak-prone enough to be worth replacing?
    insertable       can the replacement be done by insertion at all?

A 4 inch cast iron main at low pressure is still GSEP eligible and still gets
replaced - just not by insertion. Folding this into the GSEP flag would make the
eligibility counts in the output mean something other than the README says they
mean, and would quietly change what "GSEP eligible" reports across every layer.
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

# Why a main can or cannot be inserted into. Two ways in and four ways out, so a
# count of the excluded reads as the specific thing that excluded it rather than
# as one undifferentiated "too small".
REASON_INSERTABLE = "insertable"
REASON_INSERTABLE_AT_ELEVATED = "insertable_at_elevated_pressure"
REASON_TOO_SMALL = "diameter_below_minimum"
REASON_AT_MINIMUM_NOT_ELEVATED = "diameter_at_minimum_and_not_elevated"
REASON_NO_DIAMETER = "missing_diameter"
REASON_AT_MINIMUM_NO_PRESSURE = "diameter_at_minimum_and_pressure_unknown"


def insertability(nominal_diameter, pressure_psi=None):
    """(insertable, reason) for one main.

    `pressure_psi` is the main's own operating pressure, already normalised to
    PSI - `pressure.to_psi()` does that from the recorded value and its units.
    It only decides the boundary case, so it can be omitted wherever the answer
    cannot turn on it.

    A missing diameter is not insertable. There is a threshold to test and no
    value to test it against, and defaulting either way is a guess - the same
    treatment cast iron already gets in `gsep.eligibility`. A main at exactly
    the minimum with no readable pressure is excluded on the same grounds, and
    its reason names the pressure rather than the diameter as what was missing:
    the two are different data gaps and a count that merged them would hide
    which one to go and fix.
    """
    diameter = parse_number(nominal_diameter)
    if diameter is None:
        return False, REASON_NO_DIAMETER
    if diameter > config.MIN_INSERTION_DIAMETER_IN:
        return True, REASON_INSERTABLE
    if diameter < config.MIN_INSERTION_DIAMETER_IN:
        # Below the minimum, and no pressure brings it back.
        return False, REASON_TOO_SMALL

    # Exactly at the minimum: the one case pressure decides.
    psi = parse_number(pressure_psi)
    if psi is None:
        return False, REASON_AT_MINIMUM_NO_PRESSURE
    if psi > config.INSERTION_ELEVATED_PRESSURE_PSI:
        return True, REASON_INSERTABLE_AT_ELEVATED
    return False, REASON_AT_MINIMUM_NOT_ELEVATED


def is_insertable(nominal_diameter, pressure_psi=None):
    return insertability(nominal_diameter, pressure_psi)[0]


def where_clause(diameter_field="nominaldiameter",
                 pressure_field="OPERATINGPRESSURE",
                 units_field="pressureunits"):
    """The same rule as SQL, for a service-side query or a definition query.

    The pressure half is written against the recorded value and its units,
    because that is what the service stores - there is no PSI column to compare
    against. The water-column figure is the PSI threshold converted at
    `config.WC_PER_PSI`, so it moves when the threshold does.
    """
    minimum = config.MIN_INSERTION_DIAMETER_IN
    psi = config.INSERTION_ELEVATED_PRESSURE_PSI
    wc = psi * config.WC_PER_PSI
    return (
        f"({diameter_field} > {minimum:g}"
        f" OR ({diameter_field} = {minimum:g}"
        f" AND (({units_field} = {config.PRESSURE_UNIT_PSI}"
        f" AND {pressure_field} > {psi:g})"
        f" OR ({units_field} = {config.PRESSURE_UNIT_WC}"
        f" AND {pressure_field} > {wc:g}))))"
    )
