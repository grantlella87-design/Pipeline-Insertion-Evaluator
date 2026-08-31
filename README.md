# LPP GSEP Pipeline Insertion Candidate Identification

## Objective

Identify GSEP-eligible Low Pressure Pipe (LPP) systems that are candidates for insertion into nearby elevated pressure systems.

A candidate must satisfy all of the following:

- Be GSEP eligible.
- Be part of a Lower Pressure distribution system.
- Be located within 50 feet of an Other Pressure system.
- The Other Pressure system pressure must be greater than or equal to the candidate system pressure.

---

# Source Data

## MA Pressure View

**Service**

```text
https://gis.nationalgrid.com/arcgis/rest/services/MA/Pressure_View_MA/MapServer
```

### Source Layer

```text
Main Lines (Layer 145)
```

### Purpose

- Operating pressure
- Pressure classification
- Material identification
- Installation date
- Elevated pressure system identification
- Candidate system identification

### Notes

- Material View is not required.
- Main Lines contains the required attributes for this workflow.

---

# Pressure Classifications

| Classification | Definition |
|---------------|-------------|
| Lower Pressure | ≤14" WC |
| Lower Pressure | >14" WC and ≤2 PSI |
| Other Pressure | >2 PSI and ≤124 PSI |

---

# GSEP Eligibility

The production query should use coded values stored in the Main Lines layer.

## Material Codes

| Material | ASSETTYPE |
|-----------|-----------|
| Bare Steel | 1 |
| Cast Iron | 2 |
| Coated Steel | 3 |
| Copper | 5 |
| Wrought Iron | 12 |

### Fields Used

- ASSETTYPE
- nominaldiameter
- installationdate
- GLOBALID
- legacyid

---

## Combined GSEP Query

```sql
(
    (ASSETTYPE = 2 AND nominaldiameter <= 14)
    OR
    (ASSETTYPE = 1)
    OR
    (
        ASSETTYPE = 3
        AND installationdate < DATE '1971-08-01'
    )
    OR
    (ASSETTYPE = 5)
    OR
    (ASSETTYPE = 12)
)
```

### GSEP Logic Explained

A pipe is considered GSEP eligible if it is:

- Cast Iron with nominal diameter ≤ 14 inches.
- Bare Steel.
- Coated Steel installed before August 1, 1971.
- Copper.
- Wrought Iron.

Plastic eligibility should be finalized after confirmation of the applicable plastic ASSETTYPE values used by the GSEP program.

---

# Candidate Buckets

## Bucket 1 - Lower Pressure

### Criteria

```sql
GSEP_ELIGIBLE = 1
AND (
    (pressureunits = 2 AND OPERATINGPRESSURE <= 60)
    OR
    (pressureunits = 1 AND OPERATINGPRESSURE <= 2)
)
```

### Pressure Units Domain (7_UPDM_UnitsForPressure)

| Code | Unit | Bucket Usage |
|--------|--------|--------|
| 0 | Unknown | Excluded |
| 1 | Pounds/Square Inch (PSI) | Lower Pressure ≤ 2 PSI |
| 2 | Inch Water Column (WC) | Lower Pressure ≤ 60 WC catch-all threshold |

### Notes

The WC threshold is intentionally set to 60 WC to account for systems that may not have transitioned to PSI after approximately 0.5 PSI.

If `OPERATINGPRESSURE` is null:

```sql
COALESCE(OPERATINGPRESSURE, MAOPRECORD)
```

should be used as the pressure source.

### Output Layer

```text
GSEP_LPP_LowerPressure
```

---

## Bucket 2 - Other Pressure

### Criteria

```sql
pressureunits = 1
AND OPERATINGPRESSURE > 2
AND OPERATINGPRESSURE <= 124
```

### Purpose

These systems serve as potential insertion targets.

### Output Layer

```text
OtherPressureSystems
```

---

# Dissolved Pipeline Systems

Create pipeline systems by dissolving contiguous connected mains.

## Dissolve Fields

```text
OPERATINGPRESSURE
PRESSURE_BUCKET
```

### Pressure Bucket

Derived classification:

```text
Lower Pressure
Other Pressure
```

Only contiguous connected mains with the same operating pressure and pressure bucket should be dissolved together.

---

## Traceability

### SOURCE_IDS

Format:

```text
{GUID}|LegacyID;
{GUID}|LegacyID;
{GUID}|LegacyID
```

Example:

```text
{F6D95C58-43AB-4A11-BD17-102A65E9D3C2}|123456;
{31A613FC-614C-45B3-B1B9-8AF378AA5D44}|789456
```

### Purpose

- Preserve source feature references.
- Preserve GIS GUIDs.
- Preserve legacy IDs.
- Support engineering traceability.

---

# Elevated Pressure Systems

## Definition

```sql
pressureunits = 1
AND OPERATINGPRESSURE > 2
AND OPERATINGPRESSURE <= 124
```

### Output Layer

```text
ElevatedPressureSystems
```

---

# Nearest Elevated Pressure System Analysis

For each dissolved Lower Pressure system:

1. Find nearest Other Pressure system.
2. Calculate shortest distance.
3. Capture nearest point coordinates.
4. Capture target system pressure.

## Recommended Tool

```text
Near
```

or

```text
Generate Near Table
```

---

## Output Fields

| Field | Description |
|---------|---------|
| SYSTEM_ID | Lower Pressure system ID |
| PRESSURE_BUCKET | Lower Pressure |
| SYSTEM_PRESSURE | Candidate pressure |
| NEAREST_EP_ID | Nearest Other Pressure system |
| NEAREST_EP_PRESSURE | Target system pressure |
| DISTANCE_FT | Shortest distance |
| NEAR_X | Nearest X coordinate |
| NEAR_Y | Nearest Y coordinate |
| SOURCE_IDS | Traceability field |

---

# Connection Path Layer

Create a line layer representing the shortest path from each candidate Lower Pressure system to the nearest Other Pressure system.

## Purpose

- Visualize insertion opportunities.
- Display shortest connection path.
- Support engineering review.
- Support constructability assessment.

## Output Layer

```text
LPP_GSEP_InsertionPaths
```

## Suggested Fields

| Field |
|---------|
| SYSTEM_ID |
| NEAREST_EP_ID |
| DISTANCE_FT |
| SYSTEM_PRESSURE |
| NEAREST_EP_PRESSURE |
| PRESSURE_BUCKET |
| SOURCE_IDS |

---

# Final Candidate Selection

A candidate must satisfy:

```sql
DISTANCE_FT <= 50
AND NEAREST_EP_PRESSURE >= SYSTEM_PRESSURE
```

---

# Final Deliverable Layer

```text
LPP_GSEP_PipelineInsertionCandidates
```

Contents include:

- GSEP eligibility
- Lower Pressure classification
- Dissolved systems
- Traceability
- Nearest Other Pressure system
- Connection path
- Distance measurement
- Pressure comparison
- Candidate insertion locations

---

# Layer Inventory

| Purpose | Layer |
|----------|----------|
| Source Data | Pressure_View_MA |
| Source Main Layer | Main Lines (145) |
| Lower Pressure Candidates | GSEP_LPP_LowerPressure |
| Lower Pressure Systems | LPP_LowerPressure_Systems |
| Other Pressure Systems | OtherPressureSystems |
| Elevated Pressure Systems | ElevatedPressureSystems |
| Connection Paths | LPP_GSEP_InsertionPaths |
| Final Candidates | LPP_GSEP_PipelineInsertionCandidates |

---

# Implementation

The specification above is implemented in this repository. Everything down to
here is the specification; everything below describes the code that runs it.

## Running it

```text
python run.py
```

That is the whole setup. On a Python that cannot import what the workflow
needs, `run.py` creates `.venv` in the repository root, installs
`requirements.txt` into it, and re-runs itself with that interpreter - then
carries on. A first run prints a setup banner and takes a few minutes to
download the geospatial stack; later runs skip straight through in about a
tenth of a second.

```text
python run.py                    everything, then serve the map
python run.py --no-view          stop after the GeoPackage
python run.py --view-only        just serve the map
python run.py --refresh          ignore the layer cache
python run.py --port 8800        serve on another port
python run.py --no-bootstrap     use this Python as-is, install nothing

python scripts/arcgis_signin.py --test-query   # prove the token works
```

`run.py` verifies the ArcGIS token before anything long starts, downloads Main
Lines, runs the evaluation, writes the GeoPackage and opens a Leaflet map of
the result. The dependency check happens before the sign-in, so a missing
package cannot cost you an interactive authentication first.

A first run needs an ArcGIS Portal sign-in. The token is cached in the OS
credential store, so later runs need no browser.

### The environment

`bootstrap.py` can also be run on its own, and imports nothing outside the
standard library - it has to work on the interpreter that is missing
everything.

```text
python bootstrap.py            create .venv and install into it
python bootstrap.py --check    report what is missing, change nothing
python bootstrap.py --network  report what it thinks of the network
python bootstrap.py --force    reinstall even if nothing is missing
```

To manage the environment yourself - conda, a company-managed Python, a
container that already has everything - either pass `--no-bootstrap` or set
`PIPEINSERT_NO_BOOTSTRAP=1`, and `run.py` will use whatever interpreter it was
started with.

`truststore` is in `requirements.txt` for a reason worth knowing about: it
injects the OS certificate store into TLS. `auth.py` imports it inside a
try/except and runs without it, but on a corporate network whose proxy presents
an internal CA, it is the difference between every request working and every
request failing certificate verification.

### On the office network (Zscaler)

Zscaler intercepts outbound TLS, so pip cannot reach PyPI directly - the
connection is reset before it starts:

```text
WARNING: Retrying ... after connection broken by
'ProtocolError('Connection aborted.', ConnectionResetError(10054,
 'An existing connection was forcibly closed by the remote host'))': /simple/pip/
```

The bootstrap handles this without being told to. Before installing it checks
whether `http://zscaler.nationalgrid.com:80` answers, and if it does, pip is
given `--proxy` for that address.

There is a second half that is easy to miss. Once pip is going through the
proxy, the certificate PyPI appears to present is signed by Zscaler's own CA,
which certifi has never heard of - so fixing only the route turns a connection
reset into an SSL error. The bootstrap therefore also exports the Windows trust
store, where IT installed the Zscaler root, to
`.venv\corporate-ca-bundle.pem` and passes it as `--cert`. Both settings are
written into `.venv\pip.ini`, so your own `pip install` in that environment
keeps working the same way.

Check what it makes of the network without installing anything:

```text
python bootstrap.py --network
```

```text
Network check:
  proxy http://zscaler.nationalgrid.com:80: reachable
  Zscaler root certificate installed: yes (1 found)
  Zscaler client running: ZSATray.exe, ZSATunnel.exe

pip would use: http://zscaler.nationalgrid.com:80
and verify against the Windows trust store, so Zscaler's re-signed
certificates are accepted.
```

The proxy is probed rather than assumed, so the same checkout works off the
corporate network: if nothing answers, pip goes out directly. Reachability is
the right test rather than "is the user in the office" - the Zscaler client
tunnels from home too, and someone in the building on a guest network is not
behind it.

To override any of it:

| Variable | Effect |
|----------|--------|
| `PIPEINSERT_PIP_PROXY=http://host:port` | Use this proxy, skip detection |
| `PIPEINSERT_PIP_PROXY=` (empty) | Force a direct connection |
| `PIPEINSERT_ZSCALER_PROXY=http://...` | Probe a different address |
| `PIPEINSERT_PIP_CERT=C:\path\ca.pem` | Verify against this bundle instead |

An `HTTPS_PROXY` or `HTTP_PROXY` already in the environment is left alone - pip
reads those itself, and overriding them would ignore a deliberate choice.

None of this touches the workflow's own requests. `gis.nationalgrid.com` is
internal, and `auth.make_session` deliberately clears the proxy variables and
sets `NO_PROXY` for it, so ArcGIS is reached directly. Only pip, talking to
PyPI on the public internet, needs to go through Zscaler.

Not to be confused with `scripts/_bootstrap.py`, which is a different and much
smaller thing: the `sys.path` shim that lets the diagnostic scripts import the
package. It runs the venv bootstrap too, so those scripts self-install the same
way.

## Repository layout

```text
run.py                                  one command: set up, sign in, evaluate, map
bootstrap.py                            creates .venv and installs, on a bare Python
src/pipeline_insertion_evaluator.py     the workflow, stage by stage
src/leaflet_bbox_server.py              the map, served by bounding box
src/pipelineinsertion/
    config.py       every path, URL and threshold, overridable by environment
    auth.py         ArcGIS Portal OAuth and token caching
    arcgis.py       REST download, layer cache, delta refresh
    fields.py       value cleaning and field-name resolution
    domains.py      coded-value domains read from layer metadata
    gsep.py         GSEP eligibility, as a rule and as SQL
    pressure.py     pressure buckets, units, PSI conversion
    classify.py     raw Main Lines -> the two buckets
    systems.py      dissolve contiguous mains, SOURCE_IDS traceability
    nearest.py      near analysis, connection paths, candidate selection
    crs.py          choosing a coordinate system distances can be measured in
    schema.py       the layer and column names this project writes
scripts/            diagnostics: sign-in, layer description, funnel report
tests/              the rules, tested offline
```

No threshold is written into a filter expression. The GSEP material codes, the
bucket boundaries, the coated-steel cut-off and the 50 ft proximity test are all
in `config.py`, and each can be overridden with an environment variable.

## When the output looks wrong

```text
python scripts/diagnose.py
```

An empty map has several possible causes that look identical from the browser:
no GeoPackage, a download that returned nothing, a filter that matched nothing,
a dissolve that produced nothing, or coordinates in the wrong place. This walks
the same stages the workflow does and prints the count after each, so the stage
that lost the features names itself. It also reports where the data actually
sits on the earth and says so loudly when that is not Massachusetts.

It runs off the layer cache, so it needs no token and no network, and it
changes nothing. Add `--where` to print the SQL for each stage.

## Tests

```text
python -m pytest
```

379 tests, none of which need a network, an ArcGIS token or a GIS install. They
cover the eligibility rule, the pressure buckets and unit conversion, the
dissolve and its traceability field, the near analysis and the final selection,
and an end-to-end run over a small synthetic network with a known answer -
including the GeoPackage write and reading it back through the map. The
bootstrap is covered too, without creating a venv or running pip: which
interpreter is running, what is missing, that a re-exec cannot loop, and that
the Zscaler proxy is used when it answers and not when it does not.

`gsep.where_clause()` and `pressure.lower_pressure_where()` generate the SQL in
the specification above from the same `config` values the local rules use, and
the tests check the two agree. The download stage is the one part not exercised,
because it is the one part that needs a service.

---

# Notes on the specification

Where the implementation does something the specification above does not
literally say, it is listed here.

## The final pressure comparison is made in PSI

The specification states the final test as:

```sql
DISTANCE_FT <= 50
AND NEAREST_EP_PRESSURE >= SYSTEM_PRESSURE
```

Compared as raw numbers that test is wrong whenever the candidate is recorded in
water column, which is most of them. A 55" WC candidate is about 2.0 PSI, so a
5 PSI target genuinely exceeds it - but `5 >= 55` is false, and the candidate is
dropped. Because the Lower Pressure bucket admits WC values up to 60 and the
Other Pressure bucket is PSI only, this affects most of the candidate list.

Both sides are therefore converted to PSI before the comparison, at
27.7076" WC per PSI. The recorded values and their units are still written to
the output alongside the converted ones, so the conversion can be checked rather
than taken on trust.

## Systems are dissolved on connectivity, not just on attributes

The specification asks for contiguous connected mains to be dissolved together.
A dissolve on `OPERATINGPRESSURE` and `PRESSURE_BUCKET` alone merges every main
at that pressure across the state into one multipart feature, whether or not any
of them touch - and that feature has no meaningful distance to anything, so the
near analysis would be measuring against a system that is everywhere.

Mains are grouped by (bucket, pressure), and the connected components within
each group are found and dissolved one at a time. Two mains count as connected
when they come within `CONNECT_TOLERANCE_FT` (0.1 ft) of each other; exact
coordinate equality split systems that were digitised a hundredth of a foot
apart.

## Other Pressure systems are not filtered on GSEP eligibility

Bucket 1 is the candidates, and it is filtered on GSEP eligibility. Bucket 2 is
the insertion targets, and it is not: an insertion is made into whatever
elevated system is there, and that system's own material has no bearing on
whether it can receive one. Filtering the targets on GSEP too would discard most
of the elevated network and quietly shorten the candidate list.

## ElevatedPressureSystems and OtherPressureSystems are the same selection

The specification gives both the identical definition. They are written as one
selection under both of the names the layer inventory asks for, rather than two
queries that could drift apart.

## Plastic eligibility is off, not guessed

The specification leaves plastic eligibility open until the GSEP program's
plastic ASSETTYPE values are confirmed. `config.PLASTIC_ASSETTYPES` is empty, so
no plastic main is eligible, and every run says so - the candidate count is a
lower bound until those codes are filled in. Adding them to that tuple turns
plastic on in both the local rule and the generated SQL.

## Missing values are excluded, and say which value was missing

Cast iron with no nominal diameter, and coated steel with no installation date,
have a threshold to test and no value to test it against. Those mains are
excluded and their `GSEP_REASON` records which value was missing, rather than
being defaulted in either direction.

## The service's projection has no EPSG code

MA Pressure View is published in a custom projection,
`NG_Equidistant_Conic_USft`, whose `spatialReference` arrives as a bare
`{"wkt": ...}` with no `wkid`. Code that reads only `wkid`/`latestWkid` gets
nothing back, and the geometries are then labelled with the fallback zone
instead - which puts every feature about 600 miles out to sea, off South
Carolina, while raising nothing and leaving the numbers looking plausible.

`arcgis.spatial_reference_of` therefore reads the WKT before the wkid. The
projection measures in US survey feet, so the analysis keeps it and no
reprojection happens before distances are measured. When a layer has no wkid,
no `outSR` is requested either - the service answers in its own reference,
which is the one the frame is labelled with.

A layer cache written before this was fixed carries no CRS at all, and nothing
downstream can recover one. Such a cache is detected on read and re-downloaded
automatically.

## Distances need a foot-based coordinate system

The whole answer is a 50 ft threshold, so the analysis cannot run in whatever
CRS the layer arrives in. A geographic CRS measures in degrees, where 50 feet is
about 0.00014 - a threshold of 50 would accept everything. A metre-based CRS
understates every distance by 3.28, turning a 50 ft filter into a 164 ft one.
Neither raises. The layer's own spatial reference is used when it already
measures in feet, and `FALLBACK_ANALYSIS_EPSG` (2249, NAD83 / Massachusetts
Mainland ftUS) otherwise.

## Two additions to the layer inventory

| Layer | Why |
|----------|----------|
| `LPP_GSEP_NearAudit` | Every Lower Pressure system with its near result and a status saying why it did or did not qualify. "12 candidates" and "12 candidates out of 4,000 examined, 3,100 with nothing within 50 ft" are different reports, and only the second shows when the analysis has gone wrong. |
| `SOURCE_IDS` on every system layer | The specification asks for it on the near output and the connection paths. It is carried on the dissolved system layers too, so a system is traceable wherever it is opened. |

`LPP_GSEP_InsertionPaths` also carries the paths for Lower Pressure systems that
had a nearest target but did not qualify, flagged with `IS_CANDIDATE` and
`CANDIDATE_STATUS`. They are off by default on the map. A near miss is worth
seeing when reviewing why a street produced no candidates.

---

# Leftovers from the sample

`input/HL_SupplementalData.csv` and `reference/mapserver_json/` came from the
leak-relocation repository this project was built from. Nothing here reads
either of them - this workflow needs no supplemental input, and the committed
metadata it does use goes in `reference/mapserver_json/MA_Pressure_View_MA/`,
which `scripts/describe_layer.py --save` writes. They are left in place rather
than deleted; they can go whenever you are ready.
