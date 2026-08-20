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
