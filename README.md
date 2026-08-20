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
pip install -r requirements.txt

python scripts/arcgis_signin.py --test-query   # prove the token works
python run.py                                  # evaluate, then serve the map
python run.py --no-view                        # stop after the GeoPackage
python run.py --view-only                      # just serve the map
python run.py --refresh                        # ignore the layer cache
```

`run.py` verifies the ArcGIS token before anything long starts, downloads Main
Lines, runs the evaluation, writes the GeoPackage and opens a Leaflet map of
the result.

A first run needs an ArcGIS Portal sign-in. The token is cached in the OS
credential store, so later runs need no browser.

## Repository layout

```text
run.py                                  one command: sign in, evaluate, map
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
scripts/            diagnostics: sign-in, layer description
tests/              the rules, tested offline
```

No threshold is written into a filter expression. The GSEP material codes, the
bucket boundaries, the coated-steel cut-off and the 50 ft proximity test are all
in `config.py`, and each can be overridden with an environment variable.

## Tests

```text
python -m pytest
```

299 tests, none of which need a network, an ArcGIS token or a GIS install. They
cover the eligibility rule, the pressure buckets and unit conversion, the
dissolve and its traceability field, the near analysis and the final selection,
and an end-to-end run over a small synthetic network with a known answer -
including the GeoPackage write and reading it back through the map.

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
