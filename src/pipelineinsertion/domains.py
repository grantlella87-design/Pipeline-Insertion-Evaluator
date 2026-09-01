"""Reading coded-value domains out of a layer's own metadata.

ASSETTYPE and pressureunits are codes. This project decides on the codes - they
are what the production query uses, and a label can be edited on the service
without the meaning changing - but it *reports* labels, because "2" in a
candidate list tells a reviewer nothing and "Cast Iron" tells them everything.

The labels are read from the layer rather than hard-coded so that a material
renamed on the service reports its current name. `gsep.MATERIAL_LABELS` is the
fallback for when no metadata copy is available.

Two shapes have to be handled, because ArcGIS stores a domain in two places:

* a plain coded-value domain hanging off the field, which is where
  `pressureunits` keeps 7_UPDM_UnitsForPressure;
* a per-subtype domain under `types`, which is where a UPDM layer keeps
  ASSETTYPE - the valid ASSETTYPE codes depend on which ASSETGROUP subtype the
  row is in, so there is one domain per group rather than one for the layer.

For the subtype case the codes are flattened into a single {code: label} map.
That is a simplification: the same ASSETTYPE code can name different materials
under different ASSETGROUPs. It is safe here only because the codes this
project acts on come from `config`, not from the map - the map is for display -
and a code that means two things is reported as a conflict rather than being
silently resolved to whichever subtype was read last.
"""
# Absolute imports with this path setup, rather than relative imports, so the
# module also works when loaded by file path or run directly - not only when
# imported as a package member.
import os as _os
import sys as _sys

_PACKAGE_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PACKAGE_PARENT not in _sys.path:
    _sys.path.insert(0, _PACKAGE_PARENT)

import json

from pipelineinsertion import config
from pipelineinsertion.fields import parse_number
from pipelineinsertion.output import detail, warn


def _code(value):
    """A domain code as an int, or None. Domain codes in this data are integers."""
    number = parse_number(value)
    return None if number is None else int(number)


def field_domain_labels(layer_json, field_name):
    """{code: label} from the plain coded-value domain on a field, if it has one."""
    labels = {}
    for field in layer_json.get("fields", []) or []:
        if str(field.get("name", "")).lower() != str(field_name).lower():
            continue
        domain = field.get("domain") or {}
        for coded in domain.get("codedValues", []) or []:
            code = _code(coded.get("code"))
            if code is not None:
                labels[code] = coded.get("name")
    return labels


def subtype_domain_labels(layer_json, field_name):
    """{code: label} from the per-subtype domains on a field.

    A code that carries different labels under different subtypes is reported
    and left out of the map, so a display label never contradicts the subtype
    the row is actually in.
    """
    seen = {}
    conflicts = set()
    for subtype in layer_json.get("types", []) or []:
        for name, domain in (subtype.get("domains") or {}).items():
            if str(name).lower() != str(field_name).lower():
                continue
            for coded in (domain or {}).get("codedValues", []) or []:
                code = _code(coded.get("code"))
                label = coded.get("name")
                if code is None:
                    continue
                if code in seen and seen[code] != label:
                    conflicts.add(code)
                seen[code] = label

    for code in conflicts:
        warn(f"{field_name} code {code} means different things under different "
             f"ASSETGROUP subtypes. It will be reported by its code rather than "
             f"by a label that would be wrong for some rows.")
        seen.pop(code, None)
    return seen


def labels_for(layer_json, field_name):
    """{code: label} for a field, from whichever place the layer keeps it."""
    if not layer_json:
        return {}
    labels = field_domain_labels(layer_json, field_name)
    if labels:
        return labels
    return subtype_domain_labels(layer_json, field_name)


def reference_layer_json(layer_id):
    """The committed metadata copy for a layer id, or None.

    `config.REFERENCE_DIR` holds one file per layer, named layer_145_*.json.
    Reading the domains from there means the map and the diagnostic scripts can
    name a material with no token and no network. Populate it with:

        python scripts/describe_layer.py <layer url> --save
    """
    if not config.REFERENCE_DIR.is_dir():
        return None
    matches = sorted(config.REFERENCE_DIR.glob(f"layer_{int(layer_id):03d}_*.json"))
    if not matches:
        return None
    with open(matches[0], encoding="utf-8") as handle:
        return json.load(handle)


def material_labels(layer_json=None):
    """{ASSETTYPE code: material label}, from live metadata or the committed copy.

    Returns {} when neither is available, which leaves `classify` reporting the
    labels in `gsep.MATERIAL_LABELS` - correct for the five codes this project
    acts on, and blank for the rest.
    """
    layer_json = layer_json or reference_layer_json(config.MAIN_LINES_LAYER_ID)
    if not layer_json:
        detail("No layer metadata for the ASSETTYPE domain; falling back to the "
               "material labels in gsep.MATERIAL_LABELS.")
        return {}
    labels = labels_for(layer_json, "ASSETTYPE")
    detail(f"ASSETTYPE domain: {len(labels)} coded values.")
    return labels


def pressure_unit_labels(layer_json=None):
    """{pressureunits code: unit label} from 7_UPDM_UnitsForPressure.

    Read only to check the domain against the codes in `config`: a service whose
    unit domain does not match what this project assumes would put every main in
    the wrong bucket, and that is worth catching at startup rather than in a
    candidate review. See `check_pressure_units`.
    """
    layer_json = layer_json or reference_layer_json(config.MAIN_LINES_LAYER_ID)
    if not layer_json:
        return {}
    return labels_for(layer_json, "pressureunits")


def check_pressure_units(layer_json=None):
    """Warn if the layer's unit domain disagrees with the codes in `config`.

    Silent when there is no metadata to check against, or when the domain
    matches. A mismatch does not stop the run - the codes in `config` are still
    what the production query uses - but it is the single most consequential
    assumption in the workflow, so it is stated rather than assumed.
    """
    labels = pressure_unit_labels(layer_json)
    if not labels:
        return True

    expected = {
        config.PRESSURE_UNIT_PSI: ("psi", "pound"),
        config.PRESSURE_UNIT_WC: ("water", "wc", "inch"),
    }
    ok = True
    for code, keywords in expected.items():
        label = str(labels.get(code, "")).lower()
        if not label:
            warn(f"The layer's pressure-units domain has no code {code}, which "
                 f"this project reads as {'PSI' if code == 1 else 'water column'}. "
                 f"Domain: {labels}")
            ok = False
        elif not any(word in label for word in keywords):
            warn(f"The layer's pressure-units domain calls code {code} "
                 f"{label!r}, which this project reads as "
                 f"{'PSI' if code == 1 else 'water column'}. Every main would "
                 f"be bucketed on the wrong unit. Domain: {labels}")
            ok = False
    if ok:
        detail(f"Pressure-units domain matches the configured codes: {labels}")
    return ok


# --- Subtype-aware decoding ---------------------------------------------------


def assetgroup_labels(layer_json):
    """{ASSETGROUP code: subtype name} from the layer's `types`.

    ASSETGROUP is the layer's `typeIdField`, so its "domain" is the subtype
    list itself rather than a coded-value domain on the field.
    """
    if not layer_json:
        return {}
    labels = {}
    for subtype in layer_json.get("types", []) or []:
        code = _code(subtype.get("id"))
        if code is not None:
            labels[code] = subtype.get("name")
    return labels


def subtype_decoder(layer_json, field_name):
    """{(ASSETGROUP code, value code): label} for a per-subtype domain.

    The honest shape for this data. On Main Lines every ASSETGROUP carries its
    own ASSETTYPE domain - eleven of them - so a code only means something
    once you know which subtype the row is in. Flattening to {code: label}
    happens to be safe for the five GSEP codes, which mean the same thing under
    every group, but it is not safe in general: code 999 is already "UNK" under
    one group and "Unknown Type" under another, and nothing stops a future
    code from differing in a way that matters.

    Decoding on the pair costs nothing and cannot be wrong, so that is what
    `decode` does; `labels_for` stays for the places that genuinely want one
    label per code, such as checking a units domain.
    """
    decoder = {}
    if not layer_json:
        return decoder
    for subtype in layer_json.get("types", []) or []:
        group = _code(subtype.get("id"))
        for name, domain in (subtype.get("domains") or {}).items():
            if str(name).lower() != str(field_name).lower():
                continue
            for coded in (domain or {}).get("codedValues", []) or []:
                code = _code(coded.get("code"))
                if code is not None:
                    decoder[(group, code)] = coded.get("name")
    return decoder


def decode(decoder, group, code, fallback=None):
    """The label for one (subtype, value) pair.

    Falls back to a match on the code alone when the pair is not in the
    decoder - a row whose ASSETGROUP is missing or is a subtype the metadata
    copy does not cover still gets named where the code is unambiguous. When
    even that is ambiguous the code is returned as text, because a label that
    might belong to a different subtype is worse than no label.
    """
    group_code, value_code = _code(group), _code(code)
    if value_code is None:
        return fallback if fallback is not None else ""

    if (group_code, value_code) in decoder:
        return decoder[(group_code, value_code)]

    candidates = {label for (_, other), label in decoder.items()
                  if other == value_code}
    if len(candidates) == 1:
        return candidates.pop()
    if fallback is not None:
        return fallback
    return str(value_code)


def decode_series(decoder, groups, codes, fallback_labels=None):
    """`decode` over two aligned sequences, for a whole column at a time."""
    fallback_labels = fallback_labels or {}
    return [decode(decoder, group, code,
                   fallback=fallback_labels.get(_code(code)))
            for group, code in zip(groups, codes)]


def assettype_decoder(layer_json=None):
    """The (ASSETGROUP, ASSETTYPE) -> material decoder for Main Lines."""
    layer_json = layer_json or reference_layer_json(config.MAIN_LINES_LAYER_ID)
    decoder = subtype_decoder(layer_json, "ASSETTYPE")
    detail(f"ASSETTYPE decoder: {len(decoder)} (ASSETGROUP, ASSETTYPE) pairs "
           f"across {len({group for group, _ in decoder})} subtypes.")
    return decoder


def plastic_assettypes(layer_json=None):
    """{code: label} for every ASSETTYPE whose label names a plastic.

    Reported, not acted on. The README leaves plastic GSEP eligibility open
    until the program confirms which of these count, and guessing from the
    label would quietly change the candidate list. This is here so the choice
    can be made from what the service actually publishes rather than from
    memory - see `config.PLASTIC_ASSETTYPES`.
    """
    layer_json = layer_json or reference_layer_json(config.MAIN_LINES_LAYER_ID)
    terms = ("plastic", "poly", "pvc", "abs", "pe ", "hdpe", "mdpe")
    found = {}
    for (_, code), label in subtype_decoder(layer_json, "ASSETTYPE").items():
        text = str(label or "").lower()
        if any(term in text for term in terms):
            found[code] = label
    return dict(sorted(found.items()))
