"""GSEP eligibility, decided from ASSETTYPE, diameter and installation date.

A main is GSEP eligible if it is:

    Cast Iron with nominal diameter <= 14 inches
    Bare Steel
    Coated Steel installed before 1971-08-01
    Copper
    Wrought Iron

The rule is expressed twice, deliberately, and the two must agree:

* `is_eligible` decides it row by row, locally, on a downloaded layer. This is
  the one the workflow uses, because the download is filtered once and reused
  for every bucket.
* `where_clause` builds the equivalent SQL, for pushing the same rule into a
  service-side query or into a definition expression on a published layer.

`test_gsep.py` checks them against the same table of cases, so a threshold
changed in `config` cannot move one and leave the other behind.

Plastic is not handled here. The README leaves plastic eligibility open until
the GSEP program's plastic ASSETTYPE values are confirmed, and `config` carries
an empty `PLASTIC_ASSETTYPES` rather than a guess - see `plastic_is_pending`.
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
from pipelineinsertion.fields import iso_date_to_epoch_ms, parse_number, to_epoch_ms

# The label each ASSETTYPE code stands for. These are for reporting only - the
# decision is made on the code. A decoded label read from the layer's own
# subtype domain is preferred where one is available; see domains.py.
MATERIAL_LABELS = {
    config.ASSETTYPE_BARE_STEEL: "Bare Steel",
    config.ASSETTYPE_CAST_IRON: "Cast Iron",
    config.ASSETTYPE_COATED_STEEL: "Coated Steel",
    config.ASSETTYPE_COPPER: "Copper",
    config.ASSETTYPE_WROUGHT_IRON: "Wrought Iron",
}

# Why a main was ruled eligible, carried into the output so a candidate list can
# be explained without re-running the rule.
REASON_BARE_STEEL = "bare_steel"
REASON_CAST_IRON = "cast_iron_le_max_diameter"
REASON_COATED_STEEL = "coated_steel_pre_cutoff"
REASON_COPPER = "copper"
REASON_WROUGHT_IRON = "wrought_iron"
REASON_PLASTIC = "plastic"

# Why it was not.
REASON_NO_ASSETTYPE = "no_assettype"
REASON_INELIGIBLE_MATERIAL = "ineligible_material"
REASON_CAST_IRON_TOO_LARGE = "cast_iron_over_max_diameter"
REASON_CAST_IRON_NO_DIAMETER = "cast_iron_missing_diameter"
REASON_COATED_STEEL_TOO_NEW = "coated_steel_installed_after_cutoff"
REASON_COATED_STEEL_NO_DATE = "coated_steel_missing_installation_date"

ELIGIBLE_REASONS = frozenset({
    REASON_BARE_STEEL,
    REASON_CAST_IRON,
    REASON_COATED_STEEL,
    REASON_COPPER,
    REASON_WROUGHT_IRON,
    REASON_PLASTIC,
})


def plastic_is_pending():
    """True while the plastic ASSETTYPE values are still unconfirmed.

    The workflow says so once at startup, so a candidate count is never read as
    complete when a whole material class has not been decided yet.
    """
    return not config.PLASTIC_ASSETTYPES


def assettype_code(value):
    """An ASSETTYPE as an int, or None.

    Codes arrive as ints from the service and as floats once pandas has seen a
    null in the column, so 2.0 and 2 have to mean the same subtype.
    """
    number = parse_number(value)
    if number is None:
        return None
    return int(number)


def material_label(assettype):
    """The material a code stands for, or "" for one this project does not name."""
    return MATERIAL_LABELS.get(assettype_code(assettype), "")


def coated_steel_cutoff_epoch_ms():
    return iso_date_to_epoch_ms(config.COATED_STEEL_INSTALLED_BEFORE)


def eligibility(assettype, nominal_diameter=None, installation_date=None):
    """(eligible, reason) for one main.

    Both halves are returned together because the reason is what makes a
    candidate list reviewable: "not eligible" and "cast iron, but 16 inch" are
    very different answers to an engineer looking at a map.

    A missing diameter on cast iron, or a missing installation date on coated
    steel, is *not* eligible. Those two rules have a threshold to test and no
    value to test it against, and defaulting either way is a guess - so the row
    is excluded and says which value it wanted. That is the one place this
    differs in spirit from the SQL, where a NULL comparison is simply not true;
    the outcome is the same, but here it is recorded rather than silent.
    """
    code = assettype_code(assettype)
    if code is None:
        return False, REASON_NO_ASSETTYPE

    if code == config.ASSETTYPE_BARE_STEEL:
        return True, REASON_BARE_STEEL

    if code == config.ASSETTYPE_COPPER:
        return True, REASON_COPPER

    if code == config.ASSETTYPE_WROUGHT_IRON:
        return True, REASON_WROUGHT_IRON

    if code in config.PLASTIC_ASSETTYPES:
        return True, REASON_PLASTIC

    if code == config.ASSETTYPE_CAST_IRON:
        diameter = parse_number(nominal_diameter)
        if diameter is None:
            return False, REASON_CAST_IRON_NO_DIAMETER
        if diameter <= config.CAST_IRON_MAX_DIAMETER_IN:
            return True, REASON_CAST_IRON
        return False, REASON_CAST_IRON_TOO_LARGE

    if code == config.ASSETTYPE_COATED_STEEL:
        installed_ms = to_epoch_ms(installation_date)
        if installed_ms is None:
            return False, REASON_COATED_STEEL_NO_DATE
        if installed_ms < coated_steel_cutoff_epoch_ms():
            return True, REASON_COATED_STEEL
        return False, REASON_COATED_STEEL_TOO_NEW

    return False, REASON_INELIGIBLE_MATERIAL


def is_eligible(assettype, nominal_diameter=None, installation_date=None):
    return eligibility(assettype, nominal_diameter, installation_date)[0]


def where_clause(assettype_field="ASSETTYPE", diameter_field="nominaldiameter",
                 installed_field="installationdate"):
    """The same rule as SQL, for a service-side query or definition expression.

    Field names are parameters because the layer owns its spelling; the caller
    passes what `resolve_field_name` found rather than what this module assumes.
    """
    clauses = [
        f"({assettype_field} = {config.ASSETTYPE_CAST_IRON}"
        f" AND {diameter_field} <= {config.CAST_IRON_MAX_DIAMETER_IN:g})",
        f"({assettype_field} = {config.ASSETTYPE_BARE_STEEL})",
        f"({assettype_field} = {config.ASSETTYPE_COATED_STEEL}"
        f" AND {installed_field} < DATE '{config.COATED_STEEL_INSTALLED_BEFORE}')",
        f"({assettype_field} = {config.ASSETTYPE_COPPER})",
        f"({assettype_field} = {config.ASSETTYPE_WROUGHT_IRON})",
    ]
    for code in config.PLASTIC_ASSETTYPES:
        clauses.append(f"({assettype_field} = {int(code)})")
    return "(\n    " + "\n    OR\n    ".join(clauses) + "\n)"
