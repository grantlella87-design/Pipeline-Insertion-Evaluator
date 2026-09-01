"""Turning downloaded Main Lines into the two candidate buckets.

This is the bridge between the raw layer and the analysis: it applies the
per-row rules in `gsep` and `pressure` to a GeoDataFrame and writes the columns
`schema` declares. It resolves nothing and decides nothing on its own - the
field names arrive already resolved from `arcgis.resolve_fields`, and every
threshold is in `config`.

The two buckets differ in one important way that is easy to miss:

* Bucket 1, Lower Pressure, is the *candidates*, and it is filtered on GSEP
  eligibility. A pipe that is not GSEP eligible is not a candidate for GSEP
  work whatever its pressure.
* Bucket 2, Other Pressure, is the *targets*, and it is not. An insertion is
  made into whatever elevated system is there; that system's own material is
  irrelevant to whether it can receive one. Filtering the targets on GSEP too
  would discard most of the network the candidates would be inserted into and
  quietly shrink the candidate list.
"""
# Absolute imports with this path setup, rather than relative imports, so the
# module also works when loaded by file path or run directly - not only when
# imported as a package member.
import os as _os
import sys as _sys

_PACKAGE_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PACKAGE_PARENT not in _sys.path:
    _sys.path.insert(0, _PACKAGE_PARENT)

from pipelineinsertion import config, domains, gsep, pressure, schema
from pipelineinsertion.fields import clean, epoch_ms_to_iso, parse_number, to_epoch_ms
from pipelineinsertion.output import log, step, warn


def classify(gdf, resolved, domain_labels=None, layer_json=None):
    """Add the GSEP, pressure and decoded-attribute columns to a Main Lines frame.

    `resolved` is the dict from `arcgis.resolve_fields`. `layer_json` is the
    layer metadata, live or from the committed copy under `reference/`, and is
    what makes the coded values readable: ASSETTYPE is decoded on the
    (ASSETGROUP, ASSETTYPE) pair, because on this layer every ASSETGROUP
    carries its own ASSETTYPE domain. `domain_labels` is the older flat
    {code: label} form, still accepted as a fallback.

    Returns a new frame; the input is not modified.
    """
    step("Classifying mains")
    if len(gdf) == 0:
        warn("The layer download returned no mains, so every output layer will "
             "be empty.")
        return gdf.copy()

    frame = gdf.copy()
    labels = dict(domain_labels or {})
    if layer_json is None and not labels:
        layer_json = domains.reference_layer_json(config.MAIN_LINES_LAYER_ID)
    if layer_json is not None and not labels:
        labels = domains.labels_for(layer_json, "ASSETTYPE")

    assettype = frame[resolved["assettype"]]
    diameter = (frame[resolved["diameter"]] if resolved.get("diameter")
                else _blank(frame))
    installed = (frame[resolved["installed"]] if resolved.get("installed")
                 else _blank(frame))
    operating = frame[resolved["pressure"]]
    units = frame[resolved["pressure_units"]]
    maop = frame[resolved["maop"]] if resolved.get("maop") else _blank(frame)

    if not resolved.get("diameter"):
        warn("The layer has no nominal diameter field, so no cast iron main can "
             "be tested against the 14 inch limit. Every cast iron main will be "
             "reported as missing a diameter and excluded.")
    if not resolved.get("installed"):
        warn("The layer has no installation date field, so no coated steel main "
             "can be tested against the 1971 cut-off. Every coated steel main "
             "will be reported as missing a date and excluded.")
    if not resolved.get("maop"):
        warn("The layer has no MAOPRECORD field, so a main with a null "
             "operating pressure has no fallback pressure and cannot be "
             "classified.")

    eligibility = [gsep.eligibility(code, size, date)
                   for code, size, date in zip(assettype, diameter, installed)]
    frame[schema.GSEP_ELIGIBLE] = [int(bool(eligible)) for eligible, _ in eligibility]
    frame[schema.GSEP_REASON] = [reason for _, reason in eligibility]
    frame[schema.MATERIAL] = [
        labels.get(gsep.assettype_code(code)) or gsep.material_label(code)
        for code in assettype
    ]

    _add_source_attributes(frame, resolved, layer_json, labels)

    pressures = [pressure.pressure_value(value, fallback)
                 for value, fallback in zip(operating, maop)]
    frame[schema.PRESSURE] = pressures
    frame[schema.PRESSURE_FROM_MAOP] = [
        int(parse_number(value) is None and found is not None)
        for value, found in zip(operating, pressures)
    ]
    frame[schema.PRESSURE_UNITS] = [pressure.unit_code(value) for value in units]
    frame[schema.PRESSURE_UNIT_LABEL] = [pressure.unit_label(value) for value in units]
    frame[schema.PRESSURE_PSI] = [pressure.to_psi(value, unit)
                                  for value, unit in zip(pressures, units)]
    frame[schema.PRESSURE_BUCKET] = [pressure.bucket(value, unit)
                                     for value, unit in zip(pressures, units)]

    _report(frame)
    return frame


def lower_pressure_candidates(frame):
    """Bucket 1: GSEP-eligible mains in the Lower Pressure bucket."""
    selected = frame[
        (frame[schema.GSEP_ELIGIBLE] == 1)
        & (frame[schema.PRESSURE_BUCKET] == config.BUCKET_LOWER)
    ].copy()
    log(f"Bucket 1, {config.BUCKET_LOWER}: {len(selected):,} GSEP-eligible mains.")
    return selected


def other_pressure_targets(frame):
    """Bucket 2: mains in the Other Pressure bucket, whatever their material.

    Not filtered on GSEP eligibility - see the module docstring. These are the
    same mains the README publishes as ElevatedPressureSystems; the definitions
    given for the two are identical, so they are one selection written to two
    layers rather than two queries that could drift apart.
    """
    selected = frame[frame[schema.PRESSURE_BUCKET] == config.BUCKET_OTHER].copy()
    log(f"Bucket 2, {config.BUCKET_OTHER}: {len(selected):,} mains "
        f"(insertion targets; not filtered on GSEP eligibility).")
    return selected


def _blank(frame):
    import pandas as pd

    return pd.Series([None] * len(frame), index=frame.index)


def _report(frame):
    eligible = int((frame[schema.GSEP_ELIGIBLE] == 1).sum())
    log(f"Classified {len(frame):,} mains. GSEP eligible: {eligible:,}.")

    if gsep.plastic_is_pending():
        warn("Plastic ASSETTYPE values are not confirmed, so no plastic main is "
             "GSEP eligible in this run. The candidate count is a lower bound "
             "until config.PLASTIC_ASSETTYPES is filled in.")

    reasons = frame[schema.GSEP_REASON].value_counts().to_dict()
    for reason in sorted(reasons, key=lambda name: -reasons[name]):
        log(f"  {reasons[reason]:>8,}  {reason}")

    buckets = frame[schema.PRESSURE_BUCKET].value_counts().to_dict()
    unbucketed = buckets.get(pressure.BUCKET_NONE, 0)
    log(f"Pressure buckets: "
        f"{buckets.get(config.BUCKET_LOWER, 0):,} {config.BUCKET_LOWER}, "
        f"{buckets.get(config.BUCKET_OTHER, 0):,} {config.BUCKET_OTHER}, "
        f"{unbucketed:,} in neither.")

    from_maop = int(frame[schema.PRESSURE_FROM_MAOP].sum())
    if from_maop:
        log(f"{from_maop:,} mains were classified on MAOPRECORD because "
            f"OPERATINGPRESSURE was null.")

    unknown_units = int((frame[schema.PRESSURE_UNITS]
                         == config.PRESSURE_UNIT_UNKNOWN).sum())
    if unknown_units:
        warn(f"{unknown_units:,} mains record a pressure with unknown units. "
             f"They are in neither bucket: the number alone does not say "
             f"whether it is a candidate or a target.")


def _set_canonical(frame, canonical, values):
    """Set a canonical column, replacing any case-variant of it.

    A GeoPackage field name is case-insensitive, so a frame carrying both the
    layer's own `nominaldiameter` and this project's `NOMINALDIAMETER` cannot
    be written at all - GDAL rejects the second with "Error adding field", and
    says nothing about why. The source spelling is replaced rather than kept
    alongside, so every layer has exactly one column per attribute and it is
    the one `schema` declares.
    """
    for existing in list(frame.columns):
        if existing != canonical and str(existing).lower() == canonical.lower():
            frame.drop(columns=[existing], inplace=True)
    frame[canonical] = values


def _add_source_attributes(frame, resolved, layer_json, labels):
    """Carry ASSETTYPE, diameter, installation date and CP subnetwork through.

    Each coded value is written twice: the raw code, which is what the
    production query filters on and what a record traces back to, and the
    decoded label beside it, which is what a reviewer reads. Neither replaces
    the other - a label can be edited on the service without the meaning
    changing, and a bare code tells a reviewer nothing.

    ASSETTYPE is decoded on the (ASSETGROUP, ASSETTYPE) pair rather than on the
    code alone. All five GSEP codes happen to mean the same thing under every
    subtype on this layer, so a flat decode would give the same answer today -
    but code 999 is already "UNK" under one group and "Unknown Type" under
    another, and nothing stops a new code from differing in a way that matters.
    """
    assetgroup_field = resolved.get("assetgroup")
    assettype_field = resolved["assettype"]

    groups = (frame[assetgroup_field] if assetgroup_field in frame.columns
              else _blank(frame))
    codes = frame[assettype_field]

    _set_canonical(frame, schema.ASSETGROUP, list(groups))
    _set_canonical(frame, schema.ASSETTYPE, list(codes))

    group_labels = domains.assetgroup_labels(layer_json)
    _set_canonical(frame, schema.ASSETGROUP_DECODED,
                   [group_labels.get(domains._code(value), "") for value in groups])

    decoder = domains.subtype_decoder(layer_json, "ASSETTYPE")
    if decoder:
        _set_canonical(frame, schema.ASSETTYPE_DECODED, domains.decode_series(
            decoder, groups, codes, fallback_labels=labels))
    else:
        # No metadata copy: fall back to the labels this project names itself,
        # which cover the five codes it acts on and nothing else.
        warn("No layer metadata for the ASSETTYPE domain, so materials are "
             "named from gsep.MATERIAL_LABELS. Save a copy with: "
             "python scripts/describe_layer.py --save")
        _set_canonical(frame, schema.ASSETTYPE_DECODED, [
            labels.get(gsep.assettype_code(code)) or gsep.material_label(code)
            for code in codes])

    diameter_field = resolved.get("diameter")
    _set_canonical(frame, schema.NOMINAL_DIAMETER,
                   list(frame[diameter_field]) if diameter_field in frame.columns
                   else [None] * len(frame))

    installed_field = resolved.get("installed")
    installed = (frame[installed_field] if installed_field in frame.columns
                 else _blank(frame))
    _set_canonical(frame, schema.INSTALLATION_DATE, list(installed))
    # Epoch milliseconds are what the service sends and what the date rule
    # compares, but a column of them is unreadable in a desktop GIS.
    _set_canonical(frame, schema.INSTALLATION_DATE_ISO,
                   [epoch_ms_to_iso(to_epoch_ms(value)) for value in installed])

    cp_field = resolved.get("cpsubnetwork")
    _set_canonical(frame, schema.CP_SUBNETWORK,
                   [clean(value) for value in frame[cp_field]]
                   if cp_field in frame.columns else [""] * len(frame))
    if not cp_field:
        warn("The layer has no cpsubnetworkname field, so the CP subnetwork "
             "column will be blank on every layer.")
