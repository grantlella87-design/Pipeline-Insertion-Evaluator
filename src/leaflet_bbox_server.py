"""A Leaflet map of the insertion candidates, served by bounding box.

Reads the GeoPackage the evaluator writes and serves each layer as GeoJSON for
the current viewport. Bounding-box serving rather than one big GeoJSON file
because the classified-main layers are the whole GSEP network in Massachusetts,
which no browser will accept in a single document.

The map exists because a 50 ft insertion opportunity is a judgement, not a
number. A candidate with a target on the far side of a street and a candidate
with one in the same trench are both "within 50 ft"; only a picture tells them
apart, which is what makes the constructability review possible.

Layers are grouped into what the analysis produced:

    CANDIDATES   the deliverable, plus the insertion path to each target
    SYSTEMS      the dissolved Lower Pressure and Other Pressure systems
    MAINS        the classified source mains behind them

A layer whose source is missing draws as empty and says why, on the page as
well as in the terminal - a fresh checkout has no GeoPackage, and a traceback
instead of a map is not a useful answer to that.
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import geopandas as gpd

from pipelineinsertion import config, schema
from pipelineinsertion.viewer_pane import PANE_CSS, PANE_HTML, PANE_JS

HOST = "127.0.0.1"
PORT = 8765

OUTPUT_GPKG = Path(config.OUTPUT_GPKG)

# The page draws in WGS 84, which is what Leaflet expects; the analysis runs in
# a foot-based projected CRS. The reprojection happens once, at load.
DISPLAY_EPSG = 4326

MAX_FEATURES_DEFAULT = 25000
SIMPLIFY_DEFAULT = 0.000002

MAX_ZOOM = config.MAP_MAX_ZOOM
TILE_MAX_NATIVE_ZOOM = config.TILE_MAX_NATIVE_ZOOM

# Massachusetts, for the case where there is no data to fit to.
FALLBACK_BOUNDS = {"west": -73.51, "south": 41.19, "east": -69.86, "north": 42.89,
                   "center_lat": 42.05, "center_lon": -71.68}

GROUPS = {
    "candidates": {"label": "CANDIDATES"},
    "systems": {"label": "SYSTEMS"},
    "mains": {"label": "SOURCE MAINS"},
}

# One entry per GeoPackage layer. `gpkg` is the layer name the evaluator wrote;
# `default` is whether it is drawn on load. The candidates and their paths are
# on by default and everything else is off: the source mains are the whole
# network, and drawing them first hides the handful of features the map is for.
LAYERS = {
    "candidates": {
        "label": "Insertion candidates",
        "gpkg": schema.CANDIDATES_LAYER,
        "group": "candidates", "color": "#d40000", "weight": 6, "default": True,
    },
    "paths_pass": {
        "label": "Insertion paths (candidates)",
        "gpkg": schema.INSERTION_PATHS_LAYER,
        "group": "candidates", "color": "#ff8c00", "weight": 4, "default": True,
        "only_candidates": True,
    },
    "paths_fail": {
        "label": "Insertion paths (excluded)",
        "gpkg": schema.INSERTION_PATHS_LAYER,
        "group": "candidates", "color": "#9e9e9e", "weight": 2, "default": False,
        "only_candidates": False,
    },
    "lower_systems": {
        "label": "Lower Pressure systems",
        "gpkg": schema.LOWER_PRESSURE_SYSTEMS_LAYER,
        "group": "systems", "color": "#1f77b4", "weight": 3, "default": False,
    },
    "elevated_systems": {
        "label": "Elevated / Other Pressure systems",
        "gpkg": schema.ELEVATED_PRESSURE_SYSTEMS_LAYER,
        "group": "systems", "color": "#8b008b", "weight": 3, "default": False,
    },
    "lower_mains": {
        "label": "GSEP Lower Pressure mains",
        "gpkg": schema.GSEP_LOWER_PRESSURE_LAYER,
        "group": "mains", "color": "#5aa9dd", "weight": 1, "default": False,
    },
    "other_mains": {
        "label": "Other Pressure mains",
        "gpkg": schema.OTHER_PRESSURE_MAINS_LAYER,
        "group": "mains", "color": "#c48fc4", "weight": 1, "default": False,
    },
}

# Attributes worth reading first in a popup, in this order. Everything else
# follows, so a column added upstream still appears without being listed here.
POPUP_FIELD_ORDER = (
    schema.SYSTEM_ID,
    schema.CANDIDATE_STATUS,
    schema.DISTANCE_FT,
    schema.SYSTEM_PRESSURE,
    schema.SYSTEM_PRESSURE_UNITS,
    schema.SYSTEM_PRESSURE_PSI,
    schema.NEAREST_EP_ID,
    schema.NEAREST_EP_PRESSURE,
    schema.NEAREST_EP_PRESSURE_PSI,
    schema.PRESSURE_BUCKET,
    schema.MATERIAL,
    schema.GSEP_REASON,
    schema.MAIN_COUNT,
    schema.LENGTH_FT,
    schema.SOURCE_IDS,
)

DATA = {}
# Why a layer is empty, so the map explains itself rather than showing an empty
# layer and leaving the reason in the terminal.
LAYER_NOTES = {}
BOUNDS = None
LOCK = threading.Lock()


def log(value):
    print(str(value), flush=True)


# --- Loading -----------------------------------------------------------------


def available_layers():
    """The layer names in the GeoPackage, or [] when there is not one.

    Three ways to ask, because which one exists depends on the install:
    geopandas 1.0 added `list_layers`, and before that the engine had to be
    asked directly - pyogrio on a modern install, fiona on an older one.
    Hard-coding any single one of them makes the map blank on a machine that
    has the other, with every layer reported as missing from a GeoPackage that
    is sitting right there.
    """
    if not OUTPUT_GPKG.is_file():
        return []
    try:
        if hasattr(gpd, "list_layers"):
            return [str(name) for name in gpd.list_layers(OUTPUT_GPKG)["name"]]
    except Exception as ex:  # noqa: BLE001 - fall through to the engines
        log(f"WARNING geopandas.list_layers failed on {OUTPUT_GPKG}: {ex}")
    for module_name, function_name in (("pyogrio", "list_layers"),
                                       ("fiona", "listlayers")):
        try:
            import importlib

            module = importlib.import_module(module_name)
            listed = getattr(module, function_name)(str(OUTPUT_GPKG))
            # pyogrio returns an (name, geometry type) array; fiona a list of names.
            return [str(entry[0]) if isinstance(entry, (list, tuple))
                    or getattr(entry, "shape", None) else str(entry)
                    for entry in listed]
        except ImportError:
            continue
        except Exception as ex:  # noqa: BLE001 - reported, not raised
            # A readable file that will not list is reported and the map still
            # draws, with every layer explaining itself as unavailable.
            log(f"WARNING {module_name}.{function_name} failed on "
                f"{OUTPUT_GPKG}: {ex}")
            return []
    log(f"WARNING no way to list the layers in {OUTPUT_GPKG}: neither "
        f"geopandas.list_layers, pyogrio nor fiona is available.")
    return []


def read_gpkg(layer_name):
    gdf = gpd.read_file(OUTPUT_GPKG, layer=layer_name)
    if gdf.crs is None:
        log(f"WARNING {layer_name} has no CRS, so it cannot be reprojected for "
            f"display. It is drawn as if it were already lat/lon.")
        return gdf
    return gdf.to_crs(epsg=DISPLAY_EPSG)


def load_all():
    """Read every configured layer into memory, in display CRS.

    One read per distinct GeoPackage layer: the two insertion-path layers are
    two filtered views of the same one, and reading it twice would double the
    load time to draw the same features in two colours.
    """
    global BOUNDS

    present = set(available_layers())
    if not OUTPUT_GPKG.is_file():
        log(f"WARNING no GeoPackage at {OUTPUT_GPKG}. Every layer will be "
            f"empty. To build it: python run.py --no-view")

    cache = {}
    for key, cfg in LAYERS.items():
        name = cfg["gpkg"]
        if name not in present:
            note = (f"the GeoPackage has no layer {name!r}"
                    if OUTPUT_GPKG.is_file()
                    else f"there is no GeoPackage at {OUTPUT_GPKG}")
            LAYER_NOTES[key] = note + ". Run: python run.py --no-view"
            DATA[key] = _empty()
            continue

        if name not in cache:
            try:
                cache[name] = read_gpkg(name)
            except Exception as ex:  # noqa: BLE001 - one bad layer, not the map
                # A layer that will not read - a corrupt write, a GDAL version
                # that rejects it - should cost that layer and nothing else.
                log(f"WARNING could not read {name}: {ex}")
                cache[name] = _empty()
                LAYER_NOTES[key] = f"could not be read: {ex}"

        gdf = cache[name]
        if cfg.get("only_candidates") is not None and schema.IS_CANDIDATE in gdf.columns:
            wanted = bool(cfg["only_candidates"])
            gdf = gdf[gdf[schema.IS_CANDIDATE].astype(bool) == wanted].copy()
        DATA[key] = gdf
        log(f"  {cfg['label']}: {len(gdf):,} features")

    BOUNDS = compute_bounds()


def _empty():
    return gpd.GeoDataFrame({"geometry": []}, geometry="geometry",
                            crs=f"EPSG:{DISPLAY_EPSG}")


def compute_bounds():
    """The extent to open the map at: everything loaded, else Massachusetts.

    Layers whose extent is not finite are left out. One invalid geometry makes
    `total_bounds` return NaN for its whole layer, NaN then wins every min() and
    max() here, and the page is handed `fitBounds([[NaN, NaN], [NaN, NaN]])` -
    which throws inside Leaflet, kills the rest of the page's script and leaves
    every layer unloaded. A single bad row must not cost the whole map.
    """
    import math

    boxes = []
    for key, gdf in DATA.items():
        if not len(gdf):
            continue
        box = gdf.total_bounds
        if all(math.isfinite(value) for value in box):
            boxes.append(box)
        else:
            log(f"WARNING {LAYERS[key]['label']} has no finite extent, so it is "
                f"ignored when framing the map. It holds geometry with NaN "
                f"coordinates.")

    if not boxes:
        log("No layer has a usable extent, so the map opens on Massachusetts.")
        return dict(FALLBACK_BOUNDS)

    west = min(box[0] for box in boxes)
    south = min(box[1] for box in boxes)
    east = max(box[2] for box in boxes)
    north = max(box[3] for box in boxes)
    bounds = {
        "west": float(west), "south": float(south),
        "east": float(east), "north": float(north),
        "center_lat": float((south + north) / 2.0),
        "center_lon": float((west + east) / 2.0),
    }
    # Belt and braces: whatever happens above, the page is never handed a NaN,
    # because there is no recovering from it on the browser side.
    if not all(math.isfinite(value) for value in bounds.values()):
        log("WARNING the combined extent is not finite, so the map opens on "
            "Massachusetts.")
        return dict(FALLBACK_BOUNDS)
    return bounds


# --- Serving -----------------------------------------------------------------


def gdf_to_geojson(gdf):
    if len(gdf) == 0:
        return {"type": "FeatureCollection", "features": []}
    return json.loads(gdf.to_json(drop_id=True))


def select_bbox(key, west, south, east, north, simplify, max_features):
    """The features of one layer inside a viewport, capped and simplified.

    The cap is reported rather than silently applied: a truncated layer looks
    exactly like a sparse one on screen, and on this map that would read as
    "there are no candidates here".
    """
    gdf = DATA[key]
    if len(gdf) == 0:
        return gdf, False, 0
    sub = gdf.cx[west:east, south:north].copy()
    total = len(sub)
    truncated = False
    if max_features and total > max_features:
        sub = sub.head(max_features).copy()
        truncated = True
    if simplify and len(sub):
        sub["geometry"] = sub.geometry.simplify(simplify, preserve_topology=True)
        sub = sub[sub.geometry.notna() & ~sub.geometry.is_empty].copy()
    return sub, truncated, total


LEAFLET_CDN = "https://unpkg.com/leaflet@1.9.4/dist"
LEAFLET_FILES = ("leaflet.css", "leaflet.js")

CONTENT_TYPES = {".css": "text/css", ".js": "application/javascript",
                 ".png": "image/png", ".svg": "image/svg+xml"}


def guess_content_type(name):
    return CONTENT_TYPES.get(os.path.splitext(name)[1].lower(),
                             "application/octet-stream")


def leaflet_dir():
    """The folder to serve /leaflet/ from, or None.

    The copy committed in the repository is used, so the map works with no
    internet and nothing built first.
    """
    folder = config.VENDORED_LEAFLET_DIR
    if all((folder / name).exists() for name in LEAFLET_FILES):
        return folder
    return None


def leaflet_refs():
    """Where the page should load Leaflet from.

    With no local copy the page falls back to the CDN and says so. Emitting the
    local path anyway would 404 and render an empty white rectangle, the only
    clue being "L is not defined" in the browser console.
    """
    if leaflet_dir():
        return "/leaflet/leaflet.css", "/leaflet/leaflet.js"
    log(f"WARNING no local Leaflet in {config.VENDORED_LEAFLET_DIR}, so the "
        f"page will load it from unpkg.com. If this network blocks that, the "
        f"map will be blank.")
    return f"{LEAFLET_CDN}/leaflet.css", f"{LEAFLET_CDN}/leaflet.js"


def grouped_control_html():
    """The layer control's markup: a parent checkbox per group, then its children.

    Built here rather than in the page's JavaScript, where HTML quotes inside a
    JS string inside a Python string is three levels of escaping to get right.
    """
    from html import escape

    parts = []
    for group_key, group in GROUPS.items():
        children = [(key, cfg) for key, cfg in LAYERS.items()
                    if cfg.get("group") == group_key]
        if not children:
            continue
        parts.append('<div class="group">')
        parts.append(
            f'<label class="parent"><input type="checkbox" '
            f'data-group="{escape(group_key)}" checked/> '
            f'<b>{escape(group["label"])}</b></label>')
        for key, cfg in children:
            checked = " checked" if cfg.get("default") else ""
            parts.append(
                f'<label class="child"><input type="checkbox" '
                f'data-layer="{escape(key)}"{checked}/> '
                f'{escape(cfg["label"])}</label>')
        parts.append("</div>")
    return "".join(parts)


def legend_html():
    """The legend, generated from LAYERS so it cannot contradict the map.

    Written out by hand it would be a second copy of the colours, and editing
    one without the other is how a legend starts describing the previous
    version of the map.
    """
    from html import escape

    parts = []
    for cfg in LAYERS.values():
        parts.append(
            f'<span class="legend-line" style="background:{cfg["color"]}"></span>'
            f'{escape(cfg["label"])} ')
    return "".join(parts)


def html_page():
    css_ref, js_ref = leaflet_refs()
    parts = []
    parts.append('<!doctype html><html><head><meta charset="utf-8"/>')
    parts.append("<title>LPP GSEP Pipeline Insertion Candidates</title>")
    parts.append(f'<link rel="stylesheet" href="{css_ref}"/>')
    parts.append(
        "<style>html,body{height:100%;width:100%;margin:0;padding:0;"
        "font-family:Arial,sans-serif}"
        ".info{background:white;padding:10px 12px;border:1px solid #777;"
        "border-radius:4px;font-size:13px;box-shadow:0 1px 5px rgba(0,0,0,.35);"
        "max-width:790px}"
        ".warn{color:#a94442;font-weight:bold}"
        ".leaflet-control-layers{max-height:72vh;overflow:auto}"
        ".legend-line{display:inline-block;width:24px;height:4px;margin-right:6px;"
        "margin-left:8px;vertical-align:middle}"
        ".grouped-layers{padding:6px 10px;background:#fff;max-height:72vh;"
        "overflow:auto;font-size:12px}"
        ".grouped-layers .group{margin-bottom:6px;padding-bottom:4px;"
        "border-bottom:1px solid #ddd}"
        ".grouped-layers label{display:block;white-space:nowrap}"
        ".grouped-layers .child{padding-left:16px}" + PANE_CSS + "</style>")
    parts.append('</head><body><div id="map"></div>' + PANE_HTML
                 + f'<script src="{js_ref}"></script><script>' + PANE_JS)

    parts.append("const LAYER_CONFIG = " + json.dumps(LAYERS) + ";")
    parts.append("const GROUP_CONFIG = " + json.dumps(GROUPS) + ";")
    parts.append("const LAYER_NOTES = " + json.dumps(LAYER_NOTES) + ";")
    parts.append("const DATA_BOUNDS = " + json.dumps(BOUNDS) + ";")
    parts.append("const POPUP_ORDER = " + json.dumps(list(POPUP_FIELD_ORDER)) + ";")
    parts.append("const LEGEND_HTML = " + json.dumps(legend_html()) + ";")
    parts.append(f"const MAX_FEATURES={MAX_FEATURES_DEFAULT}; "
                 f"const SIMPLIFY={SIMPLIFY_DEFAULT};")
    parts.append(f"const MAX_DISTANCE_FT={config.MAX_DISTANCE_FT:g};")

    # Zoom past the tile providers' own limit. maxNativeZoom is the deepest zoom
    # each provider actually has tiles for; beyond it Leaflet upscales the last
    # tile it has instead of refusing to zoom. That matters more here than on
    # most maps: an insertion path is at most 50 feet long, which at zoom 19 is
    # a few pixels.
    parts.append(f"const map=L.map('map',{{preferCanvas:true,maxZoom:{MAX_ZOOM}}});")
    parts.append(
        "const osm=L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',"
        f"{{maxZoom:{MAX_ZOOM},maxNativeZoom:{TILE_MAX_NATIVE_ZOOM},"
        "attribution:'&copy; OpenStreetMap contributors'});")
    parts.append(
        "const esriImagery=L.tileLayer('https://services.arcgisonline.com/ArcGIS/"
        "rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',"
        f"{{maxZoom:{MAX_ZOOM},maxNativeZoom:{TILE_MAX_NATIVE_ZOOM},"
        "attribution:'Tiles &copy; Esri'});")
    parts.append(
        "const esriTopo=L.tileLayer('https://services.arcgisonline.com/ArcGIS/"
        "rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}',"
        f"{{maxZoom:{MAX_ZOOM},maxNativeZoom:{TILE_MAX_NATIVE_ZOOM},"
        "attribution:'Tiles &copy; Esri'});")
    parts.append("osm.addTo(map); const groups={}; const active=new Set(); "
                 "const status={}; const loadErrors={};")

    parts.append(
        "function esc(v){if(v===null||v===undefined)return '';"
        "return String(v).replaceAll('&','&amp;').replaceAll('<','&lt;')"
        ".replaceAll('>','&gt;')}")
    parts.append(
        # SOURCE_IDS is the one field worth wrapping: it is one line per source
        # main and a long system runs to hundreds of them.
        "function bindPopup(feature,layer){const props=feature.properties||{};"
        "let keys=[];for(const k of POPUP_ORDER){"
        "if(Object.prototype.hasOwnProperty.call(props,k))keys.push(k)}"
        "for(const k of Object.keys(props)){"
        "if(!keys.includes(k)&&keys.length<22)keys.push(k)}"
        "let rows='';for(const k of keys){"
        "let v=esc(props[k]);"
        "if(k==='SOURCE_IDS')v='<div style=\"max-height:120px;overflow:auto;"
        "font-family:monospace;font-size:11px\">'+v.replaceAll(';',';<br/>')+'</div>';"
        "rows+='<tr><th style=\"text-align:left;padding-right:8px\">'+esc(k)"
        "+'</th><td>'+v+'</td></tr>'}"
        "if(!rows)rows='<tr><td>No attributes</td></tr>';"
        "layer.bindPopup('<table>'+rows+'</table>',{maxWidth:460});"
        "layer.on('click',function(){AttributePane.selectFromMap(layer)})}")
    parts.append(
        "function styleFor(k){const c=LAYER_CONFIG[k];"
        "return {color:c.color,weight:c.weight||2,opacity:.85}}")
    parts.append(
        "for(const key of Object.keys(LAYER_CONFIG)){groups[key]=L.layerGroup();"
        "if(LAYER_CONFIG[key].default){groups[key].addTo(map);active.add(key)}"
        "AttributePane.register(key,LAYER_CONFIG[key].label,groups[key])}")

    parts.append(
        "const baseMaps={'OpenStreetMap street map':osm,"
        "'Esri World Imagery':esriImagery,'Esri World Topographic':esriTopo};"
        " L.control.layers(baseMaps,{},{collapsed:false}).addTo(map);"
        " L.control.scale({imperial:true,metric:true}).addTo(map);"
        # The data layers get a grouped control instead of Leaflet's flat list,
        # so CANDIDATES / SYSTEMS / SOURCE MAINS can be parents of anything.
        "const GROUP_CONTROL_HTML=" + json.dumps(grouped_control_html()) + ";"
        "const groupControl=L.control({position:'topright'});"
        "groupControl.onAdd=function(){"
        " const div=L.DomUtil.create('div','leaflet-control-layers grouped-layers');"
        " L.DomEvent.disableClickPropagation(div);"
        " L.DomEvent.disableScrollPropagation(div);"
        " div.innerHTML=GROUP_CONTROL_HTML;"
        " div.querySelectorAll('input[data-layer]').forEach(function(box){"
        "  box.addEventListener('change',function(){"
        "   setLayer(box.dataset.layer,box.checked); syncParents(div);});"
        " });"
        " div.querySelectorAll('input[data-group]').forEach(function(box){"
        "  box.addEventListener('change',function(){"
        "   div.querySelectorAll('input[data-layer]').forEach(function(child){"
        "    if(LAYER_CONFIG[child.dataset.layer].group!==box.dataset.group)return;"
        "    child.checked=box.checked; setLayer(child.dataset.layer,box.checked);"
        "   });"
        "   box.indeterminate=false;"
        "  });"
        " });"
        " return div;"
        "};"
        "groupControl.addTo(map);"
        # A parent is checked when any child is, so unticking the last child
        # unticks the parent instead of leaving it claiming to be on.
        "function syncParents(div){"
        " for(const gk of Object.keys(GROUP_CONFIG)){"
        "  const kids=[...div.querySelectorAll('input[data-layer]')]"
        ".filter(c=>LAYER_CONFIG[c.dataset.layer].group===gk);"
        "  const parent=div.querySelector('input[data-group=\"'+gk+'\"]');"
        "  if(parent&&kids.length){parent.checked=kids.some(c=>c.checked);"
        "   parent.indeterminate=parent.checked&&!kids.every(c=>c.checked);}"
        " }"
        "}")

    parts.append(
        "map.fitBounds([[DATA_BOUNDS.south,DATA_BOUNDS.west],"
        "[DATA_BOUNDS.north,DATA_BOUNDS.east]],{padding:[24,24]});")

    parts.append(
        # try/catch because this is the only thing that draws a layer: a failed
        # fetch otherwise surfaces as an unhandled promise rejection in the
        # console and leaves the layer silently empty.
        "async function refreshLayer(key){if(!active.has(key))return;"
        " const b=map.getBounds();"
        " const url='/api/layer?name='+encodeURIComponent(key)"
        "+'&west='+b.getWest()+'&south='+b.getSouth()+'&east='+b.getEast()"
        "+'&north='+b.getNorth()+'&simplify='+SIMPLIFY+'&max='+MAX_FEATURES;"
        " try{"
        "  const r=await fetch(url); if(!r.ok)throw new Error(r.status+' '+r.statusText);"
        "  const payload=await r.json();"
        # Close before clearing. clearLayers() destroys the layer that owns an
        # open popup and leaves the popup's DOM behind, one orphan per reload.
        "  if(map.closePopup)map.closePopup();"
        "  groups[key].clearLayers();"
        "  L.geoJSON(payload.geojson,{style:function(){return styleFor(key)},"
        "   onEachFeature:bindPopup}).addTo(groups[key]);"
        "  status[key]=payload; delete loadErrors[key];"
        " }catch(err){ loadErrors[key]=String(err); }"
        " updateInfo(); AttributePane.build();}")
    parts.append(
        "function refreshActive(){for(const key of Array.from(active))refreshLayer(key)}"
        "function setLayer(key,on){"
        " if(on){ if(!map.hasLayer(groups[key]))groups[key].addTo(map);"
        "  active.add(key); refreshLayer(key); }"
        " else { active.delete(key); groups[key].clearLayers();"
        "  if(map.hasLayer(groups[key]))map.removeLayer(groups[key]);"
        "  delete status[key]; delete loadErrors[key];"
        "  updateInfo(); AttributePane.build(); }"
        "}"
        "map.on('moveend zoomend',refreshActive);")

    parts.append(
        "const info=L.control({position:'bottomleft'});"
        "info.onAdd=function(){const div=L.DomUtil.create('div','info');"
        "div.id='infoBox';return div}; info.addTo(map);")
    parts.append(
        "function updateInfo(){const div=document.getElementById('infoBox');"
        "let html='<b>LPP GSEP pipeline insertion candidates</b><br/>'"
        "+'A candidate is a GSEP-eligible Lower Pressure system within '"
        "+MAX_DISTANCE_FT+' ft of an Other Pressure system at or above its own "
        "pressure.<br/>Click a feature for its attributes and SOURCE_IDS.<br/>'"
        "+LEGEND_HTML+'<br/><br/>';"
        "for(const key of active){"
        " if(LAYER_NOTES[key]){html+=LAYER_CONFIG[key].label"
        "+': <span class=\"warn\">not available</span> - '+esc(LAYER_NOTES[key])"
        "+'<br/>'; continue}"
        " if(loadErrors[key]){html+=LAYER_CONFIG[key].label"
        "+': <span class=\"warn\">could not load: '+esc(loadErrors[key])"
        "+'</span><br/>'; continue}"
        " const s=status[key]; if(s){html+=LAYER_CONFIG[key].label+': shown '"
        "+s.returned.toLocaleString()+' of '+s.total_in_view.toLocaleString()"
        "+' in viewport'; if(s.truncated)html+=' <span class=\"warn\">TRUNCATED"
        " - zoom in</span>'; html+='<br/>'}}"
        "div.innerHTML=html}")
    parts.append("setTimeout(refreshActive,250);</script></body></html>")
    return "\n".join(parts)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path in ["/", "/index.html"]:
                self.send_text(html_page(), "text/html")
            elif parsed.path.startswith("/leaflet/"):
                folder = leaflet_dir()
                if folder is None:
                    self.send_error(404, "No local Leaflet")
                    return
                # Resolved and checked against the folder, so a crafted path
                # cannot walk out of it. Subpaths matter: leaflet.css asks for
                # images/layers.png and images/marker-icon.png.
                relative = parsed.path[len("/leaflet/"):]
                path = (folder / relative).resolve()
                if folder.resolve() not in path.parents or not path.is_file():
                    self.send_error(404, "Missing Leaflet asset")
                    return
                self.send_bytes(path.read_bytes(), guess_content_type(path.name))
            elif parsed.path == "/api/layer":
                query = parse_qs(parsed.query)
                name = query.get("name", [""])[0]
                if name not in DATA:
                    self.send_error(400, "Unknown layer")
                    return
                west = float(query.get("west", [BOUNDS["west"]])[0])
                south = float(query.get("south", [BOUNDS["south"]])[0])
                east = float(query.get("east", [BOUNDS["east"]])[0])
                north = float(query.get("north", [BOUNDS["north"]])[0])
                simplify = float(query.get("simplify", [SIMPLIFY_DEFAULT])[0])
                max_features = int(float(query.get("max", [MAX_FEATURES_DEFAULT])[0]))
                with LOCK:
                    sub, truncated, total = select_bbox(
                        name, west, south, east, north, simplify, max_features)
                    payload = gdf_to_geojson(sub)
                self.send_text(json.dumps({
                    "name": name,
                    "returned": len(sub),
                    "total_in_view": total,
                    "truncated": truncated,
                    "geojson": payload,
                }, ensure_ascii=False, default=str), "application/json")
            else:
                self.send_error(404, "Not found")
        except Exception as ex:  # noqa: BLE001 - one request must not kill the server
            # The request handler is the outermost boundary of this process. Any
            # failure below it - a bad bbox, a frame that cannot be serialised, a
            # dropped connection - has to become a 500 for that one request and
            # nothing more; the page reports it in the info box and carries on.
            self.send_text(json.dumps({"error": str(ex)}), "application/json", 500)

    def send_text(self, text, ctype, status=200):
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_bytes(self, data, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        return


def serve(port=PORT, open_browser=False):
    """Load the layers and serve the map until interrupted."""
    log(f"Reading {OUTPUT_GPKG}")
    load_all()
    url = f"http://{HOST}:{port}/"
    log(f"OPEN THIS URL: {url}")
    log("Press Ctrl+C in this window to stop the server.")
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    with ThreadingHTTPServer((HOST, port), Handler) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            log("Stopped.")


if __name__ == "__main__":
    serve()
