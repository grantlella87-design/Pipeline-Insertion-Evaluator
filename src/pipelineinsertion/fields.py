"""Value cleaning and field-name resolution for externally-owned schemas.

The Main Lines layer's field names are not this project's to choose, and they
are not spelled consistently across the services they came from -
`OPERATINGPRESSURE`, `operatingpressure` and `Operating_Pressure` are all the
same field to a human and three different keys to a dict. Every name this code
reads out of a downloaded layer is resolved through `resolve_field_name`
against a candidate list, so a service that changes its capitalisation does not
silently produce a column of nulls.

Names this project *writes* are a different problem and are not resolved: they
are declared once in `schema.py` and read back by their exact name.
"""
# Absolute imports with this path setup, rather than relative imports, so the
# module also works when loaded by file path or run directly - not only when
# imported as a package member. spec_from_file_location gives a module no parent
# package, and a relative import then fails with "attempted relative import with
# no known parent package".
import os as _os
import sys as _sys

_PACKAGE_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PACKAGE_PARENT not in _sys.path:
    _sys.path.insert(0, _PACKAGE_PARENT)

import datetime as dt
import math
import re

_WHITESPACE = re.compile(r"\s+")
_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?")
_NON_ALNUM = re.compile(r"[^a-z0-9]")

# Strings a service uses to mean "no value". They arrive as text rather than as
# a null often enough that treating them as data puts the word "None" into an
# output column.
_NULL_TEXT = {"none", "null", "nan", "<null>", "n/a", "na", "unknown"}


def clean(value):
    """A trimmed string, with the service's spellings of null collapsed to "".

    Whitespace inside the value is collapsed too, so "Cast   Iron" and
    "Cast Iron" compare equal.
    """
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = _WHITESPACE.sub(" ", str(value)).strip()
    if text.lower() in _NULL_TEXT:
        return ""
    return text


def upper(value):
    return clean(value).upper()


def normalize_key(value):
    """A join key stripped of the decorations ArcGIS puts on identifiers.

    GLOBALIDs arrive braced from one endpoint and bare from another, and a
    numeric legacy id arrives as 123 from the service and 123.0 once pandas has
    seen a null in the column. Joining on the raw values silently matched
    nothing.
    """
    text = clean(value)
    if not text:
        return ""
    text = text.strip("{}").strip()
    if text.endswith(".0") and text[:-2].lstrip("-").isdigit():
        text = text[:-2]
    return text.upper()


def parse_number(value):
    """The first number in a value, or None.

    Diameters arrive as 4, "4", "4 IN" and "4\"" depending on the record, so the
    number is extracted rather than cast.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        return float(value)
    text = clean(value)
    if not text:
        return None
    match = _NUMBER.search(text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def simplify_field_name(name):
    """A field name reduced to what makes two spellings the same field."""
    return _NON_ALNUM.sub("", str(name).lower())


def resolve_field_name(field_names, candidates):
    """The first candidate present in `field_names`, compared loosely.

    Returns the name as the layer actually spells it, so the caller indexes with
    a key that exists. None when no candidate is present - the caller decides
    whether that is fatal, because for some fields it is and for others the
    column is simply carried as blank.
    """
    available = {simplify_field_name(name): name for name in field_names if name}
    for candidate in candidates:
        simplified = simplify_field_name(candidate)
        if simplified in available:
            return available[simplified]
    return None


def to_epoch_ms(value):
    """A date value as epoch milliseconds, or None.

    ArcGIS returns dates as epoch milliseconds already; a cache round-trip or a
    hand-edited CSV can turn them into strings or datetimes. All three are
    accepted so an installation date is comparable however it arrived.
    """
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, dt.datetime):
        when = value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
        return int(when.timestamp() * 1000)
    if isinstance(value, dt.date):
        when = dt.datetime(value.year, value.month, value.day, tzinfo=dt.timezone.utc)
        return int(when.timestamp() * 1000)
    text = clean(value)
    if not text:
        return None
    try:
        import pandas as pd

        parsed = pd.to_datetime(text, utc=True)
        if pd.isna(parsed):
            return None
        return int(parsed.timestamp() * 1000)
    except (ImportError, TypeError, ValueError, OverflowError):
        # pandas raises DateParseError and OutOfBoundsDatetime for unparseable
        # or out-of-range input, and both descend from ValueError. Without
        # pandas installed there is nothing left to try.
        return None


def iso_date_to_epoch_ms(text):
    """An ISO date string as epoch milliseconds, at UTC midnight.

    Used for the configured coated-steel cut-off, which is a plain date rather
    than an instant: comparing it in local time would move the boundary by a
    day for anyone east of Greenwich.
    """
    when = dt.datetime.strptime(str(text).strip(), "%Y-%m-%d")
    return int(when.replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


def epoch_ms_to_iso(epoch_ms):
    """Epoch milliseconds as an ISO date, for the audit columns. "" when absent."""
    if epoch_ms is None:
        return ""
    try:
        return dt.datetime.fromtimestamp(
            float(epoch_ms) / 1000.0, dt.timezone.utc).date().isoformat()
    except (OSError, OverflowError, TypeError, ValueError):
        return ""
