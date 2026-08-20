"""Print what an ArcGIS layer actually has: fields, dates, coded-value domains.

    python scripts/describe_layer.py                       # Main Lines (145)
    python scripts/describe_layer.py https://.../MapServer/145 --save

Every field name this project uses was read out of service metadata rather than
guessed, which is why `reference/` exists. This is the tool that reads it.

--save writes the layer JSON into reference/, where `domains.py` reads the
ASSETTYPE and pressureunits domains from it with no token and no network, and
where the tests can pin against it.
"""
import argparse
import json
import sys

from _bootstrap import config

from pipelineinsertion import arcgis, auth, domains
from pipelineinsertion.fields import resolve_field_name
from pipelineinsertion.output import fail, log, warn

# What the evaluator needs from Main Lines, and why.
INTERESTING = {
    "material identification": arcgis.ASSETTYPE_CANDIDATES,
    "subtype (domain selector)": arcgis.ASSETGROUP_CANDIDATES,
    "diameter (cast iron rule)": arcgis.DIAMETER_CANDIDATES,
    "installed (coated steel rule)": arcgis.INSTALLED_CANDIDATES,
    "operating pressure": arcgis.PRESSURE_CANDIDATES,
    "pressure units": arcgis.PRESSURE_UNITS_CANDIDATES,
    "MAOP (pressure fallback)": arcgis.MAOP_CANDIDATES,
    "identity": arcgis.GLOBALID_CANDIDATES,
    "traceability (legacy id)": arcgis.LEGACYID_CANDIDATES,
    "delta cache": arcgis.MODIFIED_FIELD_CANDIDATES,
}

REQUIRED = set(arcgis.REQUIRED_GROUPS)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", nargs="?", default=config.MAIN_LINES_URL,
                        help="Layer URL. Default: Main Lines on MA Pressure View.")
    parser.add_argument("--save", action="store_true",
                        help="Write the layer JSON into reference/ for the tests.")
    return parser.parse_args(argv)


def describe(layer_json, url):
    fields = layer_json.get("fields") or []
    names = [f.get("name") for f in fields if f.get("name")]

    log(f"Layer: {layer_json.get('name')!r}  id={layer_json.get('id')}")
    log(f"Geometry: {layer_json.get('geometryType')}")
    log(f"Fields: {len(names)}")
    log(f"typeIdField: {layer_json.get('typeIdField')!r}")

    log("\nWhat the evaluator looks for:")
    for purpose, candidates in INTERESTING.items():
        found = resolve_field_name(names, candidates)
        if found:
            mark = "ok  "
        else:
            mark = "FAIL" if purpose in REQUIRED else "miss"
        log(f"  {mark} {purpose:28} -> {found or '(nothing matched)'}")

    dates = [f["name"] for f in fields
             if str(f.get("type") or "") == "esriFieldTypeDate"]
    log(f"\nDate fields ({len(dates)}): {sorted(dates)}")

    assettypes = domains.labels_for(layer_json, "ASSETTYPE")
    log(f"\nASSETTYPE coded values: {len(assettypes)}")
    for code in sorted(assettypes)[:20]:
        marker = " <- GSEP" if _is_gsep_code(code) else ""
        log(f"  {code:>4}  {assettypes[code]!r}{marker}")
    if len(assettypes) > 20:
        log(f"  ... {len(assettypes) - 20} more")

    units = domains.labels_for(layer_json, "pressureunits")
    log(f"\npressureunits coded values: {len(units)}")
    for code in sorted(units):
        log(f"  {code:>4}  {units[code]!r}")
    if units and not domains.check_pressure_units(layer_json):
        warn("The unit domain does not match the codes this project assumes. "
             "Every main would be bucketed on the wrong unit.")

    log(f"\nAll field names:\n{sorted(names)}")
    log(f"\nSource: {url}")


def _is_gsep_code(code):
    return code in {
        config.ASSETTYPE_BARE_STEEL, config.ASSETTYPE_CAST_IRON,
        config.ASSETTYPE_COATED_STEEL, config.ASSETTYPE_COPPER,
        config.ASSETTYPE_WROUGHT_IRON,
    } or code in config.PLASTIC_ASSETTYPES


def save(layer_json, url):
    layer_id = layer_json.get("id")
    name = str(layer_json.get("name") or "layer").replace(" ", "_")
    config.REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = config.REFERENCE_DIR / f"layer_{int(layer_id):03d}_{name}.json"
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(layer_json, handle, indent=1)
    log(f"Wrote {path}")
    log(f"  from {url}")


def main(argv=None):
    args = parse_args(argv)
    session = auth.make_session()
    layer_json = arcgis.request_json(session, args.url.rstrip("/"), {"f": "json"})
    if not layer_json or "fields" not in layer_json:
        fail(f"{args.url} returned no field list. Response keys: "
             f"{sorted(layer_json or {})}")
    describe(layer_json, args.url)
    if args.save:
        save(layer_json, args.url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
