"""ArcGIS REST reads: metadata, paged download, and the local layer cache.

Extracted so the analysis modules stay free of HTTP. Nothing in here knows what
a GSEP main is; it downloads a layer and hands back a GeoDataFrame.

Three things make a download of Main Lines survivable:

* OBJECTIDs are requested first, then the features are fetched in batches by id
  over several connections. Paging with resultOffset against a layer this size
  is both slower and, on a layer being edited underneath the query, capable of
  skipping records as the offsets shift.
* The result is cached to a gzipped pickle next to a metadata sidecar. A repeat
  run reads the cache.
* A cache is refreshed by delta where the layer has a last-modified field: only
  records changed since the cached watermark are re-downloaded and upserted.
  The merged count is checked against the server count, because a delta cannot
  see deletes - a mismatch forces the full download.

The cache's sidecar also records the set of fields that were requested when it
was written. Adding a field to `OUT_FIELD_GROUPS` does not change the cached
data, and a delta refresh would bring the new field in for the handful of
changed records and leave it blank everywhere else - which looks like a service
that stopped populating it rather than a stale cache. A changed field set
invalidates the cache instead.
"""
# Absolute imports with this path setup, rather than relative imports, so the
# module also works when loaded by file path or run directly - not only when
# imported as a package member.
import os as _os
import sys as _sys

_PACKAGE_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PACKAGE_PARENT not in _sys.path:
    _sys.path.insert(0, _PACKAGE_PARENT)

import datetime as dt
import hashlib
import json
import math
import os
import re
from concurrent import futures

import geopandas as gpd
import pandas as pd
import requests
from shapely.errors import ShapelyError
from shapely.geometry import LineString, MultiLineString, Point, shape

from pipelineinsertion import config
from pipelineinsertion.auth import (
    clear_cached_access_token,
    get_arcgis_token,
)
from pipelineinsertion.fields import parse_number, resolve_field_name
from pipelineinsertion.output import detail, fail, log, step, warn

# --- The fields this project asks Main Lines for -----------------------------
#
# Named as candidate groups because the spelling belongs to the service. Each
# group resolves to the first name the layer actually has; a group that resolves
# to nothing is reported by the caller, which knows what its absence means.

MODIFIED_FIELD_CANDIDATES = ("LASTUPDATE", "last_edited_date", "EDITDATE",
                             "DATEMODIFIED", "MODIFIEDDATE")
OBJECTID_CANDIDATES = ("OBJECTID", "objectid", "FID", "OID")
GLOBALID_CANDIDATES = ("GLOBALID", "globalid", "GlobalID")
LEGACYID_CANDIDATES = ("legacyid", "LEGACYID", "LegacyID", "LEGACY_ID")

# The material identification, and the two attributes the GSEP rule tests
# against it.
ASSETTYPE_CANDIDATES = ("ASSETTYPE", "assettype")
ASSETGROUP_CANDIDATES = ("ASSETGROUP", "assetgroup")
DIAMETER_CANDIDATES = ("nominaldiameter", "NOMINALDIAMETER", "nominal_diameter",
                       "outsidediameter")
INSTALLED_CANDIDATES = ("installationdate", "INSTALLATIONDATE", "installdate",
                        "inservicedate")

# Pressure and its unit. Both are required to classify a main into a bucket -
# the number on its own does not say which bucket it belongs in.
PRESSURE_CANDIDATES = ("OPERATINGPRESSURE", "operatingpressure")
PRESSURE_UNITS_CANDIDATES = ("pressureunits", "PRESSUREUNITS", "unitsforpressure")
MAOP_CANDIDATES = ("MAOPRECORD", "maoprecord", "maop", "MAOP", "maopdesign")

# The cathodic protection subnetwork a main belongs to. Not part of any rule -
# carried through to the output because an insertion candidate is reviewed
# against the CP scheme it would join, and looking that up per candidate
# afterwards is the kind of manual step this workflow exists to remove.
CPSUBNETWORK_CANDIDATES = ("cpsubnetworkname", "CPSUBNETWORKNAME",
                           "cp_subnetwork_name", "cpsubnetwork")

OUT_FIELD_GROUPS = (
    OBJECTID_CANDIDATES,
    GLOBALID_CANDIDATES,
    LEGACYID_CANDIDATES,
    ASSETTYPE_CANDIDATES,
    ASSETGROUP_CANDIDATES,
    DIAMETER_CANDIDATES,
    INSTALLED_CANDIDATES,
    PRESSURE_CANDIDATES,
    PRESSURE_UNITS_CANDIDATES,
    MAOP_CANDIDATES,
    CPSUBNETWORK_CANDIDATES,
    MODIFIED_FIELD_CANDIDATES,
)

# Without one of these the workflow cannot produce a result at all, so their
# absence is fatal rather than a warning and a column of nulls.
REQUIRED_GROUPS = {
    "ASSETTYPE (material identification)": ASSETTYPE_CANDIDATES,
    "operating pressure": PRESSURE_CANDIDATES,
    "pressure units": PRESSURE_UNITS_CANDIDATES,
}


# --- Requests ----------------------------------------------------------------


def request_json(session, url, params=None):
    """A GET against the REST API, retried once through a fresh token.

    Codes 498 and 499 are "invalid token" and "token required". They are the one
    error worth retrying: a cached token that expired mid-run is the normal way
    a long download fails, and re-authenticating costs less than starting over.
    """
    request_params = dict(params or {})

    token = getattr(session, "_arcgis_access_token", None)
    if not token:
        token = get_arcgis_token(session)
        session._arcgis_access_token = token
    if token and "token" not in request_params:
        request_params["token"] = token

    response = session.get(url, params=request_params,
                           timeout=config.REQUEST_TIMEOUT_SECONDS,
                           verify=config.VERIFY_SSL)
    response.raise_for_status()
    data = response.json()

    if "error" in data:
        error = data.get("error", {})
        if str(error.get("code")) in ("498", "499"):
            warn("ArcGIS access token was rejected. Clearing the cached token "
                 "and forcing one fresh login.")
            clear_cached_access_token()
            if hasattr(session, "_arcgis_access_token"):
                delattr(session, "_arcgis_access_token")
            request_params["token"] = get_arcgis_token(session)
            session._arcgis_access_token = request_params["token"]

            response = session.get(url, params=request_params,
                                   timeout=config.REQUEST_TIMEOUT_SECONDS,
                                   verify=config.VERIFY_SSL)
            response.raise_for_status()
            data = response.json()
            if "error" in data:
                fail(f"ArcGIS REST error after a refreshed token from {url}: "
                     f"{json.dumps(data.get('error', {}), indent=2)}")
        else:
            fail(f"ArcGIS REST error from {url}: {json.dumps(error, indent=2)}")

    return data


def request_json_post(session, url, params):
    """A POST against the REST API. Used for id batches.

    An objectIds list of a few thousand ids exceeds what a URL can carry, and a
    server that truncates the query string returns a partial answer rather than
    an error - so batch fetches always POST.
    """
    request_params = dict(params or {})

    token = request_params.get("token") or getattr(session, "_arcgis_access_token", None)
    if not token:
        token = get_arcgis_token(session)
        session._arcgis_access_token = token
    if token and "token" not in request_params:
        request_params["token"] = token

    response = session.post(url, data=request_params,
                            timeout=config.REQUEST_TIMEOUT_SECONDS,
                            verify=config.VERIFY_SSL)
    if response.status_code >= 400:
        fail(f"ArcGIS POST failed for {url}. HTTP {response.status_code}. "
             f"Response snippet: {(response.text or '')[:1000]}")

    data = response.json()
    if "error" in data:
        error = data.get("error", {})
        if str(error.get("code")) in ("498", "499"):
            warn("ArcGIS access token was rejected during POST. Clearing the "
                 "cached token and forcing one fresh login.")
            clear_cached_access_token()
            if hasattr(session, "_arcgis_access_token"):
                delattr(session, "_arcgis_access_token")
            request_params["token"] = get_arcgis_token(session)
            session._arcgis_access_token = request_params["token"]

            response = session.post(url, data=request_params,
                                    timeout=config.REQUEST_TIMEOUT_SECONDS,
                                    verify=config.VERIFY_SSL)
            if response.status_code >= 400:
                fail(f"ArcGIS POST failed after a refreshed token for {url}. "
                     f"HTTP {response.status_code}. Response snippet: "
                     f"{(response.text or '')[:1000]}")
            data = response.json()
            if "error" in data:
                fail(f"ArcGIS REST POST error after a refreshed token from "
                     f"{url}: {json.dumps(data.get('error', {}), indent=2)}")
        else:
            fail(f"ArcGIS REST POST error from {url}: {json.dumps(error, indent=2)}")

    return data


# --- Metadata ----------------------------------------------------------------


def spatial_reference_of(layer_json):
    """(crs, wkid, raw) for a layer, reading the WKT before the wkid.

    The WKT comes first because the National Grid services are published in a
    custom projection - NG_Equidistant_Conic_USft - which has no EPSG code at
    all. Its spatialReference arrives as a bare {"wkt": ...}, so code that only
    looks for `wkid`/`latestWkid` gets None and hands the geometries on with no
    coordinate system.

    Nothing raises when that happens, which is what makes it worth this much
    care: the frame is simply labelled with the fallback zone later, the
    coordinates are then feet in one projection being read as feet in another,
    and the numbers stay plausible while every position is wrong.

    `crs` is whatever pyproj can consume - an "EPSG:xxxx" string or the WKT
    itself. None means the layer told us nothing.
    """
    raw = ((layer_json.get("extent") or {}).get("spatialReference")
           or layer_json.get("spatialReference") or {})
    wkid = raw.get("latestWkid") or raw.get("wkid")
    wkt = raw.get("wkt") or raw.get("wkt2")

    # A custom projection is identified only by its WKT, and where both are
    # present the WKT is the more specific of the two.
    if wkt:
        return wkt, wkid, raw
    if wkid:
        return f"EPSG:{int(wkid)}", int(wkid), raw
    return None, None, raw


def layer_metadata(session, layer_url):
    data = request_json(session, layer_url, {"f": "json"})
    fields = data.get("fields", [])
    object_id_field = data.get("objectIdField") or next(
        (f.get("name") for f in fields if f.get("type") == "esriFieldTypeOID"), None)
    layer_crs, wkid, spatial_reference = spatial_reference_of(data)
    if layer_crs is None:
        warn(f"{layer_url} reported no usable spatial reference "
             f"({spatial_reference}). Every coordinate from it is unlabelled, "
             f"so the distance analysis cannot be trusted.")
    else:
        detail(f"Layer spatial reference: wkid={wkid} "
               f"{'(custom WKT)' if not wkid else ''}")

    max_record_count = int(data.get("maxRecordCount") or config.REQUEST_PAGE_SIZE)
    page_size = (min(config.REQUEST_PAGE_SIZE, max_record_count)
                 if max_record_count > 0 else config.REQUEST_PAGE_SIZE)
    return {
        "object_id_field": object_id_field,
        "wkid": wkid,
        "crs": layer_crs,
        "spatial_reference": spatial_reference,
        "fields": fields,
        "page_size": page_size,
        "layer_json": data,
    }


def metadata_field_names(meta):
    return [field.get("name") for field in meta.get("fields", []) if field.get("name")]


def resolve_fields(field_names, layer_name):
    """Every field this project reads, resolved to the layer's own spelling.

    Returns a dict keyed by the purpose rather than by the field name, so the
    rest of the code asks for "assettype" and gets whatever the layer calls it.
    Missing required fields are fatal here, at the point where the field list is
    in hand and the error can name what the layer does have.
    """
    resolved = {
        "objectid": resolve_field_name(field_names, OBJECTID_CANDIDATES),
        "globalid": resolve_field_name(field_names, GLOBALID_CANDIDATES),
        "legacyid": resolve_field_name(field_names, LEGACYID_CANDIDATES),
        "assettype": resolve_field_name(field_names, ASSETTYPE_CANDIDATES),
        "assetgroup": resolve_field_name(field_names, ASSETGROUP_CANDIDATES),
        "diameter": resolve_field_name(field_names, DIAMETER_CANDIDATES),
        "installed": resolve_field_name(field_names, INSTALLED_CANDIDATES),
        "pressure": resolve_field_name(field_names, PRESSURE_CANDIDATES),
        "pressure_units": resolve_field_name(field_names, PRESSURE_UNITS_CANDIDATES),
        "maop": resolve_field_name(field_names, MAOP_CANDIDATES),
        "cpsubnetwork": resolve_field_name(field_names, CPSUBNETWORK_CANDIDATES),
        "modified": resolve_field_name(field_names, MODIFIED_FIELD_CANDIDATES),
    }

    missing = [purpose for purpose, candidates in REQUIRED_GROUPS.items()
               if resolve_field_name(field_names, candidates) is None]
    if missing:
        fail(f"{layer_name}: the layer has no field for {missing}, so the "
             f"workflow cannot classify a main. Fields: {sorted(field_names)}")

    for purpose, name in sorted(resolved.items()):
        detail(f"{layer_name}: {purpose} -> {name}")
    return resolved


def build_out_fields(meta, layer_name):
    field_names = metadata_field_names(meta)
    wanted = []
    for group in OUT_FIELD_GROUPS:
        resolved = resolve_field_name(field_names, group)
        if resolved and resolved not in wanted:
            wanted.append(resolved)
    if not wanted:
        warn(f"{layer_name}: could not narrow outFields; requesting all fields.")
        return "*"
    out_fields = ",".join(wanted)
    detail(f"{layer_name}: using outFields={out_fields}")
    return out_fields


def out_field_request_signature():
    """A digest of the field names this code asks for. Stored with a cache.

    Built from the request configuration alone, so it can be computed without
    contacting the service, and it changes only when the set of requested names
    changes. Case and order within a group are normalised out: a rename the
    resolver would treat as the same name must not invalidate every cache.
    """
    canonical = [sorted(name.lower() for name in group) for group in OUT_FIELD_GROUPS]
    payload = json.dumps(canonical, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


# --- Geometry ----------------------------------------------------------------


def esri_polyline_to_geom(geometry):
    if not geometry or "paths" not in geometry:
        return None
    lines = []
    for path in geometry.get("paths", []):
        coords = [(float(x), float(y)) for x, y, *rest in path]
        if len(coords) >= 2:
            lines.append(LineString(coords))
    if not lines:
        return None
    return lines[0] if len(lines) == 1 else MultiLineString(lines)


def esri_geometry_to_shape(geometry):
    if not geometry:
        return None
    if "x" in geometry and "y" in geometry:
        if geometry.get("x") is None or geometry.get("y") is None:
            return None
        return Point(float(geometry["x"]), float(geometry["y"]))
    if "paths" in geometry:
        return esri_polyline_to_geom(geometry)
    try:
        return shape(geometry)
    except (AttributeError, KeyError, TypeError, ValueError, ShapelyError):
        # Everything a malformed geometry dict raises: a missing "coordinates"
        # is a KeyError, a non-dict an AttributeError, bad coordinate values a
        # TypeError or ValueError, and an unknown "type" a GeometryTypeError -
        # which descends from ShapelyError, not from TypeError, so it has to be
        # named.
        return None


# --- Cache -------------------------------------------------------------------


def safe_cache_name(layer_name):
    return re.sub(r"[^A-Za-z0-9_]+", "_", str(layer_name).strip().lower()).strip("_")


def layer_cache_paths(layer_name):
    config.LAYER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    base = safe_cache_name(layer_name)
    return (config.LAYER_CACHE_DIR / f"{base}.pkl.gz",
            config.LAYER_CACHE_DIR / f"{base}.meta.json")


def read_layer_cache(layer_name, layer_url, where_clause):
    if not config.USE_LAYER_CACHE or config.FORCE_LAYER_REFRESH:
        return None, None
    data_path, meta_path = layer_cache_paths(layer_name)
    if not data_path.is_file() or not meta_path.is_file():
        log(f"{layer_name}: no local cache found.")
        return None, None
    try:
        with open(meta_path, encoding="utf-8") as handle:
            meta = json.load(handle)
        if meta.get("layer_url") != layer_url:
            log(f"{layer_name}: cache URL changed. Refreshing layer.")
            return None, None
        if meta.get("where_clause") != where_clause:
            log(f"{layer_name}: cache WHERE changed. Refreshing layer.")
            return None, None
        if meta.get("out_field_signature") != out_field_request_signature():
            # Returning None sends the caller down the full-download path, which
            # is the only one that can bring a new column in for every record.
            log(f"{layer_name}: the requested fields have changed since this "
                f"cache was written. Refreshing in full so the new fields are "
                f"populated for every record.")
            return None, None
        gdf = pd.read_pickle(data_path, compression="gzip")
        if len(gdf) and gdf.crs is None:
            # A cache written before the spatial reference was read from the
            # layer's WKT carries no CRS, and nothing downstream can recover
            # one - the coordinates get labelled with the fallback zone and
            # every position comes out wrong while the numbers stay plausible.
            # Refreshing is the only fix, and it has to happen without the user
            # having to know any of that.
            log(f"{layer_name}: this cache has no coordinate system, so it was "
                f"written before the layer's projection was read correctly. "
                f"Refreshing it in full.")
            return None, None
        log(f"{layer_name}: loaded {len(gdf):,} records from cache: {data_path}")
        return gdf, meta
    except Exception as ex:  # noqa: BLE001 - see below
        # Deliberately broad. Unpickling a cache written by a different pandas,
        # geopandas or Python raises whatever that payload's classes raise,
        # including ModuleNotFoundError and AttributeError, and a truncated file
        # raises EOFError or a gzip error. No cache is worth ending a run for:
        # the error is reported and the layer is refreshed from the service.
        warn(f"{layer_name}: failed to read cache, so it will be refreshed in "
             f"full. Error={ex}")
        return None, None


def write_layer_cache(layer_name, layer_url, where_clause, server_count,
                      modified_field, gdf):
    if not config.USE_LAYER_CACHE:
        return
    data_path, meta_path = layer_cache_paths(layer_name)
    max_modified_ms = max_modified_epoch_ms(gdf, modified_field)
    try:
        gdf.to_pickle(data_path, compression="gzip")
        meta = {
            "layer_name": layer_name,
            "layer_url": layer_url,
            "where_clause": where_clause,
            "server_count": int(server_count) if server_count is not None else None,
            "modified_field": modified_field,
            "max_modified_epoch_ms": (int(max_modified_ms)
                                      if max_modified_ms is not None else None),
            "cached_utc": dt.datetime.now(dt.timezone.utc).isoformat().replace(
                "+00:00", "Z"),
            "record_count_written": len(gdf),
            "out_field_signature": out_field_request_signature(),
        }
        with open(meta_path, "w", encoding="utf-8") as handle:
            json.dump(meta, handle, indent=2)
        log(f"{layer_name}: wrote cache: {data_path}")
    except Exception as ex:  # noqa: BLE001 - the cache is an optimisation
        # Pickling a frame can fail on an unpicklable column, and the write
        # itself on a full disk or a permission problem. A run that has already
        # done the work should finish, just without leaving a cache behind.
        warn(f"{layer_name}: failed to write cache. Error={ex}")


def cache_age_seconds(layer_name, layer_url, where_clause):
    """Age of a usable cache in seconds, or None if there is not one."""
    if not config.USE_LAYER_CACHE or config.FORCE_LAYER_REFRESH:
        return None
    data_path, meta_path = layer_cache_paths(layer_name)
    if not data_path.is_file() or not meta_path.is_file():
        return None
    try:
        with open(meta_path, encoding="utf-8") as handle:
            meta = json.load(handle)
        if (meta.get("layer_url") != layer_url
                or meta.get("where_clause") != where_clause):
            return None
        cached_utc = meta.get("cached_utc")
        if not cached_utc:
            return None
        stamp = dt.datetime.fromisoformat(cached_utc.replace("Z", "+00:00"))
        return max(0.0, (dt.datetime.now(dt.timezone.utc) - stamp).total_seconds())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as ex:
        # A cache whose age cannot be read is treated as absent.
        detail(f"{layer_name}: could not read cache age ({ex})")
        return None


def max_modified_epoch_ms(gdf, modified_field):
    if (gdf is None or len(gdf) == 0 or not modified_field
            or modified_field not in gdf.columns):
        return None
    values = [parse_number(value) for value in gdf[modified_field].tolist()]
    values = [int(value) for value in values if value is not None]
    return max(values) if values else None


def epoch_ms_to_sql_timestamp(epoch_ms):
    """A delta watermark as a SQL timestamp literal.

    Anything unusable falls back to the epoch, which makes the delta window the
    whole history - slower than it needs to be, but never wrong. A watermark
    that cannot be trusted must not become a window that skips records.
    """
    safe_default = "timestamp '1970-01-01 00:00:00'"
    value = parse_number(epoch_ms)
    if value is None or not math.isfinite(value) or value <= 0:
        warn(f"Delta watermark [{epoch_ms}] is unusable. Falling back to the "
             f"full safe delta window.")
        return safe_default
    if value > 32503680000000.0:  # year 3000, in milliseconds
        warn(f"Delta watermark [{epoch_ms}] is outside the valid millisecond "
             f"range. Falling back to the full safe delta window.")
        return safe_default
    try:
        when = dt.datetime.fromtimestamp(
            value / 1000.0, dt.timezone.utc).replace(tzinfo=None)
    except (OSError, OverflowError, ValueError) as ex:
        # A watermark outside the platform's representable range raises
        # OverflowError, or OSError on Windows for pre-epoch values.
        warn(f"Could not convert the delta watermark [{epoch_ms}]: {ex}. "
             f"Falling back to the full safe delta window.")
        return safe_default
    return f"timestamp '{when.strftime('%Y-%m-%d %H:%M:%S')}'"


def build_delta_where(base_where, modified_field, last_epoch_ms):
    return (f"({base_where}) AND {modified_field} > "
            f"{epoch_ms_to_sql_timestamp(last_epoch_ms)}")


def upsert_cached_layer(cached_gdf, delta_gdf, object_id_field):
    if delta_gdf is None or len(delta_gdf) == 0:
        return cached_gdf
    if (object_id_field not in cached_gdf.columns
            or object_id_field not in delta_gdf.columns):
        warn("Cannot upsert the delta because the OBJECTID field is missing. "
             "Keeping the cached layer, since returning the delta alone would "
             "silently drop every unchanged record.")
        return cached_gdf
    delta_ids = set(delta_gdf[object_id_field].astype(str).tolist())
    keep = cached_gdf[~cached_gdf[object_id_field].astype(str).isin(delta_ids)].copy()
    merged = pd.concat([keep, delta_gdf], ignore_index=True)
    return gpd.GeoDataFrame(merged, geometry="geometry",
                            crs=cached_gdf.crs or delta_gdf.crs)


# --- Download ----------------------------------------------------------------


def query_count(session, layer_url, where_clause, layer_name):
    data = request_json(session, layer_url + "/query",
                        {"f": "json", "where": where_clause,
                         "returnCountOnly": "true"})
    count = data.get("count")
    if count is None:
        warn(f"{layer_name}: returnCountOnly did not return a count.")
        return None
    log(f"{layer_name}: server-side count for WHERE [{where_clause}] = {int(count):,}")
    return int(count)


def query_object_ids(session, layer_url, where_clause, layer_name):
    log(f"{layer_name}: requesting matching OBJECTIDs for WHERE [{where_clause}]")
    data = request_json(session, layer_url + "/query",
                        {"f": "json", "where": where_clause,
                         "returnIdsOnly": "true"})
    object_ids = sorted(int(value) for value in
                        (data.get("objectIds") or data.get("objectids") or []))
    log(f"{layer_name}: returnIdsOnly returned {len(object_ids):,} OBJECTIDs")
    return object_ids


def chunk_list(values, chunk_size):
    for index in range(0, len(values), chunk_size):
        yield values[index:index + chunk_size]


def _fetch_objectid_batch(layer_url, object_id_batch, layer_name, meta, out_fields,
                          batch_number, batch_total, token):
    local_session = requests.Session()
    local_session._arcgis_access_token = token
    params = {
        "f": "json",
        "objectIds": ",".join(str(value) for value in object_id_batch),
        "outFields": out_fields,
        "returnGeometry": "true",
        "token": token,
    }
    # Omitted when the layer has no wkid: the service then answers in its own
    # native reference, which is exactly what source_crs above describes. Asking
    # for a projection the layer does not have would silently reproject.
    if meta.get("wkid"):
        params["outSR"] = meta["wkid"]
    data = request_json_post(local_session, layer_url + "/query", params)
    batch = data.get("features", [])
    detail(f"{layer_name}: batch {batch_number:,}/{batch_total:,} returned "
           f"{len(batch):,} features")
    return batch


def query_feature_set(session, layer_url, where_clause, layer_name, meta, out_fields):
    # Whatever the layer is actually published in, WKT included - not only a
    # spatial reference that happens to have an EPSG code. See
    # `spatial_reference_of`.
    source_crs = meta.get("crs")

    if config.USE_OBJECTID_BATCH_DOWNLOAD:
        object_ids = query_object_ids(session, layer_url, where_clause, layer_name)
        if not object_ids:
            log(f"{layer_name}: no OBJECTIDs matched. Returning an empty layer.")
            return gpd.GeoDataFrame([], geometry=[], crs=source_crs)

        token = getattr(session, "_arcgis_access_token", None)
        if not token:
            token = get_arcgis_token(session)
            session._arcgis_access_token = token

        batches = list(chunk_list(object_ids, config.OBJECTID_BATCH_SIZE))
        log(f"{layer_name}: downloading {len(object_ids):,} records in "
            f"{len(batches):,} POST batches with "
            f"{config.OBJECTID_DOWNLOAD_WORKERS} workers")

        features = []
        with futures.ThreadPoolExecutor(
                max_workers=config.OBJECTID_DOWNLOAD_WORKERS) as pool:
            submitted = {
                pool.submit(_fetch_objectid_batch, layer_url, batch, layer_name,
                            meta, out_fields, number, len(batches), token): number
                for number, batch in enumerate(batches, start=1)
            }
            for done, future in enumerate(futures.as_completed(submitted), start=1):
                number = submitted[future]
                try:
                    features.extend(future.result())
                except Exception as ex:  # noqa: BLE001 - re-raised from a worker
                    # future.result() re-raises whatever the download raised, so
                    # narrowing here would mean listing every failure mode of
                    # the request path. It becomes one fatal message naming the
                    # batch.
                    fail(f"{layer_name}: batch {number:,}/{len(batches):,} "
                         f"failed: {ex}")
                if done == 1 or done % 10 == 0 or done == len(batches):
                    log(f"{layer_name}: {done:,}/{len(batches):,} batches, "
                        f"{len(features):,} features")
    else:
        features = _query_by_paging(session, layer_url, where_clause, layer_name,
                                    meta, out_fields)

    rows, geometries = [], []
    for feature in features:
        rows.append(feature.get("attributes", {}) or {})
        geometries.append(esri_geometry_to_shape(feature.get("geometry")))

    if not rows:
        return gpd.GeoDataFrame([], geometry=[], crs=source_crs)

    gdf = gpd.GeoDataFrame(rows, geometry=geometries, crs=source_crs)
    dropped = int(gdf.geometry.isna().sum())
    if dropped:
        warn(f"{layer_name}: {dropped:,} records had no usable geometry and "
             f"cannot take part in a distance analysis. They are dropped.")
    return gdf[gdf.geometry.notna()].copy()


def _query_by_paging(session, layer_url, where_clause, layer_name, meta, out_fields):
    page_size = meta["page_size"]
    offset, features = 0, []
    while True:
        params = {
            "f": "json", "where": where_clause, "outFields": out_fields,
            "returnGeometry": "true", "resultOffset": offset,
            "resultRecordCount": page_size,
            "orderByFields": meta["object_id_field"],
        }
        if meta.get("wkid"):
            params["outSR"] = meta["wkid"]
        data = request_json(session, layer_url + "/query", params)
        batch = data.get("features", [])
        if not batch:
            break
        features.extend(batch)
        log(f"{layer_name}: fetched {len(features):,} records")
        if len(batch) < page_size and not data.get("exceededTransferLimit"):
            break
        offset += page_size
    return features


def query_layer(session, layer_url, where_clause, layer_name):
    """Download a layer, using and refreshing the local cache. Returns (gdf, meta).

    `meta` is the layer metadata, which the caller needs for the field
    resolution and the native spatial reference. It is returned even when the
    cache answered the data, so a cached run resolves fields the same way a
    fresh one does.
    """
    step(f"Querying ArcGIS REST layer: {layer_name}")

    # A recent cache is trusted without contacting the server. Checking costs a
    # metadata request plus two count queries and needs a valid token, which
    # dominates start-up when the data has not changed. The metadata request is
    # still made, because the caller needs the field list.
    meta = layer_metadata(session, layer_url)
    if not meta["object_id_field"]:
        fail(f"Could not determine the object id field for {layer_name}.")

    age = cache_age_seconds(layer_name, layer_url, where_clause)
    if (age is not None and config.CACHE_FRESH_SECONDS > 0
            and age < config.CACHE_FRESH_SECONDS):
        cached_gdf, _ = read_layer_cache(layer_name, layer_url, where_clause)
        if cached_gdf is not None:
            log(f"{layer_name}: cache is {age / 60:.0f} min old; skipping the "
                f"server check. Set FORCE_LAYER_REFRESH=1 to refresh.")
            return cached_gdf, meta

    object_id_field = meta["object_id_field"]
    modified_field = resolve_field_name(metadata_field_names(meta),
                                        MODIFIED_FIELD_CANDIDATES)
    if not modified_field:
        warn(f"{layer_name}: no last-modified field, so a delta refresh is not "
             f"possible. The cache will be validated by record count only.")
    out_fields = build_out_fields(meta, layer_name)
    server_count = query_count(session, layer_url, where_clause, layer_name)

    cached_gdf, cached_meta = read_layer_cache(layer_name, layer_url, where_clause)

    if cached_gdf is not None and modified_field and cached_meta:
        merged = _try_delta_refresh(
            session, layer_url, where_clause, layer_name, meta, out_fields,
            cached_gdf, cached_meta, modified_field, object_id_field, server_count)
        if merged is not None:
            return merged, meta
    elif cached_gdf is not None and (server_count is None
                                     or len(cached_gdf) == int(server_count)):
        log(f"{layer_name}: using the cache by record count, because the layer "
            f"has no last-modified field.")
        return cached_gdf, meta

    log(f"{layer_name}: performing a full layer download.")
    gdf = query_feature_set(session, layer_url, where_clause, layer_name, meta,
                            out_fields)
    write_layer_cache(layer_name, layer_url, where_clause, server_count,
                      modified_field, gdf)
    return gdf, meta


def _try_delta_refresh(session, layer_url, where_clause, layer_name, meta,
                       out_fields, cached_gdf, cached_meta, modified_field,
                       object_id_field, server_count):
    """The cached layer brought up to date, or None to fall through to a full one."""
    last_epoch_ms = cached_meta.get("max_modified_epoch_ms")
    if not last_epoch_ms:
        return None

    delta_where = build_delta_where(where_clause, modified_field, last_epoch_ms)
    log(f"{layer_name}: attempting a delta refresh WHERE [{delta_where}]")
    delta_count = query_count(session, layer_url, delta_where, layer_name + " delta")

    if delta_count == 0:
        if server_count is None or len(cached_gdf) == int(server_count):
            log(f"{layer_name}: no changes since the cache was written.")
            return cached_gdf
        warn(f"{layer_name}: nothing modified, but the counts differ "
             f"(cached={len(cached_gdf):,}, server={int(server_count):,}). "
             f"Records were deleted, which a delta cannot see. Full refresh.")
        return None

    delta_gdf = query_feature_set(session, layer_url, delta_where,
                                  layer_name + " delta", meta, out_fields)
    merged = upsert_cached_layer(cached_gdf, delta_gdf, object_id_field)
    if server_count is not None and len(merged) != int(server_count):
        warn(f"{layer_name}: the merged cache holds {len(merged):,} records "
             f"against a server count of {int(server_count):,}. Records were "
             f"deleted, which a delta cannot see. Full refresh.")
        return None

    log(f"{layer_name}: merged {len(delta_gdf):,} changed records into the cache.")
    write_layer_cache(layer_name, layer_url, where_clause, server_count,
                      modified_field, merged)
    return merged
