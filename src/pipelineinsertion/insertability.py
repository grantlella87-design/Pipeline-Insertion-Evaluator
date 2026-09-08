"""Whether a main is physically big enough to insert into.

Insertion threads a new plastic carrier pipe inside the existing main, so below
a certain bore there is no room for one. `config.MIN_INSERTION_DIAMETER_IN`
holds the threshold and the test is "greater than", so 4 inch itself is out.

Deliberately not part of `gsep.py`, and deliberately not folded into
GSEP_ELIGIBLE. The two answer different questions:

    GSEP eligible    is this main leak-prone enough to be worth replacing?
    insertable       can the replacement be done by insertion at all?

A 4 inch cast iron main is still GSEP eligible and still gets replaced - just
not by insertion. Folding this into the GSEP flag would make the eligibility
counts in the output mean something other than the README says they mean, and
would quietly change what "GSEP eligible" reports across every layer.
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

# Why a main can or cannot be inserted into.
REASON_INSERTABLE = "insertable"
REASON_TOO_SMALL = "diameter_at_or_below_minimum"
REASON_NO_DIAMETER = "missing_diameter"


def insertability(nominal_diameter):
    """(insertable, reason) for one main.

    A missing diameter is not insertable. There is a threshold to test and no
    value to test it against, and defaulting either way is a guess - the same
    treatment cast iron already gets in `gsep.eligibility`. The reason records
    which value was wanted, so a large count of these reads as a data gap
    rather than as a fleet of small mains.
    """
    diameter = parse_number(nominal_diameter)
    if diameter is None:
        return False, REASON_NO_DIAMETER
    if diameter > config.MIN_INSERTION_DIAMETER_IN:
        return True, REASON_INSERTABLE
    return False, REASON_TOO_SMALL


def is_insertable(nominal_diameter):
    return insertability(nominal_diameter)[0]


def where_clause(diameter_field="nominaldiameter"):
    """The same rule as SQL, for a service-side query or a definition query."""
    return f"({diameter_field} > {config.MIN_INSERTION_DIAMETER_IN:g})"
