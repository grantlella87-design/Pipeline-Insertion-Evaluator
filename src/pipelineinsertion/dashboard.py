"""A one-page dashboard over the GeoPackage the workflow writes.

Answers, in the order someone asks them: how many candidates are there, what
did the other systems fail on, how binding is the 50 ft threshold, what are the
candidates made of, how old are they, and which specific systems are they.

Charts are hand-built inline SVG with no script and no external library. This
project already vendors Leaflet so the map works on a machine with no internet,
and a dashboard that fetched a charting library from a CDN would be the one
deliverable that did not. The file it writes is self-contained: it can be
emailed, opened from a share, or committed to a review folder, and it will draw
the same everywhere.

Colour follows one rule throughout. Every bar chart here is a single series, so
every bar is the same hue - a value ramp across nominal categories would burn
the only free channel on the length the bar already shows. The one ramp is on
the funnel, whose stages are genuinely ordered, and it is a single-hue ordinal
ramp validated against both surfaces.
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
from html import escape
from pathlib import Path

from pipelineinsertion import config, dashboard_metrics, schema
from pipelineinsertion.output import log, step, warn

# Every layer the dashboard reads. A missing one costs its panels, not the page.
DASHBOARD_LAYERS = (
    schema.NEAR_AUDIT_TABLE,
    schema.CANDIDATES_LAYER,
    schema.GSEP_LOWER_PRESSURE_LAYER,
    schema.OTHER_PRESSURE_MAINS_LAYER,
    schema.LOWER_PRESSURE_SYSTEMS_LAYER,
    schema.ELEVATED_PRESSURE_SYSTEMS_LAYER,
)

# The ordinal ramp for the funnel, light then dark. Both runs pass the ordinal
# checks - monotone lightness, visible step gaps, light end clear of the
# surface - against their own surface. The dark column is the same hue stepped
# for the dark surface, not the light column flipped.
FUNNEL_RAMP_LIGHT = ("#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#104281")
FUNNEL_RAMP_DARK = ("#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95")

DEFAULT_FILENAME = "LPP_GSEP_InsertionDashboard.html"


def dashboard_path(gpkg=None):
    """Where the dashboard is written: beside the GeoPackage it describes."""
    gpkg = Path(gpkg or config.OUTPUT_GPKG)
    return gpkg.parent / DEFAULT_FILENAME


def read_layers(gpkg=None):
    """{layer name: frame or None} for every layer the dashboard reads."""
    import geopandas as gpd

    gpkg = Path(gpkg or config.OUTPUT_GPKG)
    if not gpkg.is_file():
        warn(f"There is no GeoPackage at {gpkg}, so there is nothing to "
             f"report on. Run: python run.py --no-view")
        return {}

    try:
        available = set(gpd.list_layers(gpkg)["name"])
    except Exception as ex:  # noqa: BLE001 - reported, not raised
        warn(f"Could not list the layers in {gpkg}: {ex}")
        return {}

    layers = {}
    for name in DASHBOARD_LAYERS:
        if name not in available:
            layers[name] = None
            continue
        try:
            layers[name] = gpd.read_file(gpkg, layer=name)
        except Exception as ex:  # noqa: BLE001 - one layer, not the page
            warn(f"Could not read {name}: {ex}")
            layers[name] = None
    return layers


# --- Formatting --------------------------------------------------------------


def _count(value):
    return "—" if value is None else f"{int(value):,}"


def _decimal(value, places=1, suffix=""):
    return "—" if value is None else f"{value:,.{places}f}{suffix}"


def _miles(feet):
    """Feet as miles. "—" when the length could not be measured at all."""
    return "—" if feet is None else f"{feet / 5280.0:,.1f} mi"


def _feet(feet):
    return "not measurable" if feet is None else f"{feet:,.0f} ft"


def _percent(value):
    return "—" if value is None else f"{value * 100:.1f}%"


# --- Chart primitives --------------------------------------------------------
#
# Marks are thin, the grid is a hairline one shade off the surface, and no bar
# carries a number on every point - values are direct-labelled at the bar end,
# which is the one place they do not collide.


def _bar_rows(rows, value_format=_count):
    """A horizontal bar chart as HTML, one series, one colour.

    Built from divs rather than SVG: a bar chart is a list with a length
    channel, the labels wrap and the widths are percentages, and CSS does all
    of that without a viewBox to get wrong at an unexpected page width.
    """
    if not rows:
        return '<p class="empty">No data for this panel.</p>'
    largest = max((count for _, count in rows), default=0) or 1
    parts = ['<div class="bars">']
    for label, count in rows:
        width = 100.0 * count / largest
        parts.append(
            '<div class="bar-row">'
            f'<div class="bar-label" title="{escape(str(label))}">'
            f'{escape(str(label))}</div>'
            '<div class="bar-track">'
            f'<div class="bar-fill" style="width:{width:.2f}%"></div>'
            '</div>'
            f'<div class="bar-value">{value_format(count)}</div>'
            '</div>')
    parts.append("</div>")
    return "".join(parts)


def _funnel(stages):
    """The workflow's stages as an ordinal ramp.

    The one place a ramp is correct here: these stages are ordered, and the
    ramp says so. Each stage is labelled with its own count rather than a
    percentage of the first - the stages are not nested subsets of one another
    (the target counts are a different population from the candidate counts),
    and a percentage would imply they were.
    """
    if not stages:
        return '<p class="empty">No data for this panel.</p>'
    largest = max((count for _, count in stages), default=0) or 1
    parts = ['<div class="funnel">']
    for index, (label, count) in enumerate(stages):
        width = max(6.0, 100.0 * count / largest)
        parts.append(
            '<div class="funnel-row">'
            f'<div class="funnel-label">{escape(label)}</div>'
            '<div class="funnel-track">'
            f'<div class="funnel-fill" style="width:{width:.2f}%;'
            f'background:var(--ramp-{index + 1})"></div>'
            f'<span class="funnel-value">{_count(count)}</span>'
            '</div></div>')
    parts.append("</div>")
    return "".join(parts)


def _length_bars(rows, stats):
    """Candidate lengths by bin, with the mean and median marked.

    Each bin shows its system count as the bar and its footage beside it: "how
    many systems" and "how much main" are different questions, and the second
    is the one that sizes the work.

    Mean and median are drawn as labelled rules rather than a second colour.
    They disagree - system lengths are heavily right-skewed, so a few long runs
    pull the mean above the median - and that disagreement is worth seeing,
    which a single "average" would hide.
    """
    if not rows or all(count == 0 for _, count, _ in rows):
        return '<p class="empty">No candidate has a measured length.</p>'

    largest = max(count for _, count, _ in rows) or 1
    parts = ['<div class="bars">']
    for label, count, footage in rows:
        width = 100.0 * count / largest
        parts.append(
            '<div class="bar-row wide">'
            f'<div class="bar-label">{escape(label)}</div>'
            '<div class="bar-track">'
            f'<div class="bar-fill" style="width:{width:.2f}%"></div>'
            '</div>'
            f'<div class="bar-value">{_count(count)}</div>'
            f'<div class="bar-value muted">{_decimal(footage, 0, " ft")}</div>'
            '</div>')
    parts.append("</div>")

    marks = []
    if stats.get("median") is not None:
        marks.append(f"median {stats['median']:,.0f} ft")
    if stats.get("mean") is not None:
        marks.append(f"mean {stats['mean']:,.0f} ft")
    if stats.get("min") is not None and stats.get("max") is not None:
        marks.append(f"range {stats['min']:,.0f}–{stats['max']:,.0f} ft")
    if marks:
        parts.append('<div class="stat-rules">'
                     + "".join(f"<span>{escape(mark)}</span>" for mark in marks)
                     + "</div>")
    return "".join(parts)


def _histogram(rows, threshold_ft):
    """Distance bins, with the threshold drawn where it actually falls.

    One colour for every bar. The pass/fail split is carried by a labelled rule
    rather than a second hue, so the chart needs no legend and the threshold
    stays legible if the page is printed in greyscale.
    """
    if not rows or all(count == 0 for _, count, _ in rows):
        return '<p class="empty">No system had a measurable distance.</p>'
    largest = max(count for _, count, _ in rows) or 1
    parts = ['<div class="bars histogram">']
    for label, count, upper in rows:
        width = 100.0 * count / largest
        crossed = upper is not None and upper == threshold_ft
        parts.append(
            '<div class="bar-row">'
            f'<div class="bar-label">{escape(label)}</div>'
            '<div class="bar-track">'
            f'<div class="bar-fill" style="width:{width:.2f}%"></div>'
            '</div>'
            f'<div class="bar-value">{_count(count)}</div>'
            '</div>')
        if crossed:
            parts.append(
                '<div class="threshold">'
                f'<span>{threshold_ft:g} ft — the candidate threshold; '
                f'everything below this line is too far</span></div>')
    parts.append("</div>")
    return "".join(parts)


def _distance_scan(rows, threshold_ft, search_limit_ft):
    """A what-if control over the distance threshold, and its readouts.

    The one interactive thing on the page. Everything else reports the run as
    it was configured; this section, and only this section, answers "what if
    the threshold moved" - so the control sits at its top and scopes what is
    below it, rather than silently changing numbers elsewhere.

    The rows are embedded rather than recomputed on a server, because the page
    has to keep working as a file: emailed, opened from a share, or read on a
    machine that has never run the workflow.

    Pressure is deliberately not relaxable. Distance is a threshold someone can
    argue about; a target below the candidate's own pressure does not become
    usable by moving a number, so the readout separates the two.
    """
    if not rows:
        return ('<p class="empty">No system had a nearest target, so there is '
                'nothing to scan.</p>')

    import json

    top = min(float(search_limit_ft), max(row[0] for row in rows))
    top = max(top, float(threshold_ft))
    return (
        '<div class="scan">'
        '<div class="controls">'
        '<label for="scanRange">Distance to nearest target</label>'
        f'<input id="scanRange" type="range" min="0" max="{top:.0f}" step="1" '
        f'value="{threshold_ft:g}" aria-describedby="scanReadout"/>'
        f'<input id="scanValue" type="number" min="0" max="{top:.0f}" step="1" '
        f'value="{threshold_ft:g}" aria-label="Distance in feet"/>'
        '<span class="unit">ft</span>'
        '<button id="scanReset" type="button">Reset to '
        f'{threshold_ft:g} ft</button>'
        '</div>'
        '<div class="scan-tiles" id="scanReadout">'
        '<div class="tile"><div class="value" id="scanCount">—</div>'
        '<div class="label">Insertable systems</div>'
        '<div class="foot">within the distance and at or below target pressure</div>'
        '</div>'
        '<div class="tile"><div class="value" id="scanFootage">—</div>'
        '<div class="label">Insertable main</div>'
        '<div class="foot" id="scanFootageFt">—</div></div>'
        '<div class="tile"><div class="value" id="scanReach">—</div>'
        '<div class="label">Within the distance</div>'
        '<div class="foot">ignoring pressure — not all of it is insertable</div>'
        '</div>'
        '<div class="tile"><div class="value" id="scanDelta">—</div>'
        f'<div class="label">Against {threshold_ft:g} ft</div>'
        '<div class="foot" id="scanDeltaFt">—</div></div>'
        '</div>'
        '<table class="scan-table"><caption>At a few round distances</caption>'
        '<thead><tr><th>Distance</th><th>Systems</th><th>Insertable main</th>'
        '<th>vs configured</th></tr></thead>'
        '<tbody id="scanSteps"></tbody></table>'
        '</div>'
        f'<script id="scanData" type="application/json">{json.dumps(rows)}</script>'
    )


def _table(headers, rows, caption):
    if not rows:
        return '<p class="empty">No candidates to list.</p>'
    parts = [f'<table><caption>{escape(caption)}</caption><thead><tr>']
    for header in headers:
        parts.append(f"<th>{escape(header)}</th>")
    parts.append("</tr></thead><tbody>")
    for row in rows:
        parts.append("<tr>")
        for cell in row:
            text = str(cell)
            # SOURCE_IDS runs to hundreds of references on a long system. It is
            # the reason the table is worth having, so it is kept - in a cell
            # that scrolls rather than one that pushes every other column off
            # the page.
            css = ' class="wrap"' if len(text) > 60 else ""
            parts.append(f"<td{css}>{escape(text)}</td>")
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


# --- The page ----------------------------------------------------------------


# The page's only script. Inline and self-contained - no fetch, no library, no
# network - so the file still behaves when it is emailed or opened from a share.
# Every number it shows is also in the GeoPackage; this only re-totals rows that
# are already on the page.
SCAN_SCRIPT = """
(function () {
  var node = document.getElementById('scanData');
  if (!node) return;
  var rows;
  try { rows = JSON.parse(node.textContent); } catch (e) { return; }
  if (!rows || !rows.length) return;

  var range = document.getElementById('scanRange');
  var value = document.getElementById('scanValue');
  var reset = document.getElementById('scanReset');
  var configured = parseFloat(range.getAttribute('value')) || 0;

  function fmt(n, digits) {
    return n.toLocaleString(undefined, {
      minimumFractionDigits: digits || 0, maximumFractionDigits: digits || 0 });
  }

  // rows are [distance_ft, length_ft, pressure_ok]
  function totals(limit) {
    var count = 0, footage = 0, reach = 0, reachFt = 0;
    for (var i = 0; i < rows.length; i++) {
      if (rows[i][0] > limit) continue;
      reach += 1; reachFt += rows[i][1];
      if (rows[i][2]) { count += 1; footage += rows[i][1]; }
    }
    return { count: count, footage: footage, reach: reach, reachFt: reachFt };
  }

  var baseline = totals(configured);

  function setText(id, text) {
    var el = document.getElementById(id);
    if (el) el.textContent = text;   // textContent, never innerHTML
  }

  function render(limit) {
    var t = totals(limit);
    setText('scanCount', fmt(t.count));
    setText('scanFootage', fmt(t.footage / 5280, 2) + ' mi');
    setText('scanFootageFt', fmt(t.footage) + ' ft of main');
    setText('scanReach', fmt(t.reach) + ' systems');
    var delta = t.footage - baseline.footage;
    var sign = delta > 0 ? '+' : (delta < 0 ? '\u2212' : '');
    setText('scanDelta', sign + fmt(Math.abs(delta) / 5280, 2) + ' mi');
    setText('scanDeltaFt', sign + fmt(Math.abs(delta)) + ' ft against the '
      + fmt(configured) + ' ft the run used');

    var body = document.getElementById('scanSteps');
    if (!body) return;
    body.textContent = '';
    var steps = [10, 25, 50, 75, 100, 150, 250];
    if (steps.indexOf(limit) === -1) steps.push(limit);
    steps.sort(function (a, b) { return a - b; });
    for (var i = 0; i < steps.length; i++) {
      var s = totals(steps[i]);
      var d = s.footage - baseline.footage;
      var tr = document.createElement('tr');
      if (steps[i] === limit) tr.className = 'current';
      [fmt(steps[i]) + ' ft', fmt(s.count),
       fmt(s.footage) + ' ft',
       (d > 0 ? '+' : (d < 0 ? '\u2212' : '')) + fmt(Math.abs(d)) + ' ft'
      ].forEach(function (text) {
        var td = document.createElement('td');
        td.textContent = text;
        tr.appendChild(td);
      });
      body.appendChild(tr);
    }
  }

  function apply(raw) {
    var limit = parseFloat(raw);
    if (!isFinite(limit) || limit < 0) limit = 0;
    var max = parseFloat(range.max);
    if (limit > max) limit = max;
    range.value = limit;
    value.value = limit;
    render(limit);
  }

  range.addEventListener('input', function () { apply(range.value); });
  value.addEventListener('input', function () { apply(value.value); });
  if (reset) reset.addEventListener('click', function () { apply(configured); });
  apply(configured);
})();
"""

STYLE = """
:root{
  color-scheme: light;
  --page:#f9f9f7; --surface:#fcfcfb;
  --ink:#0b0b0b; --ink-2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,0.10);
  --series-1:#2a78d6;
  --track:#eceae4;
  --good:#0ca30c; --warning:#fab219;
  --ramp-1:#86b6ef; --ramp-2:#5598e7; --ramp-3:#2a78d6;
  --ramp-4:#1c5cab; --ramp-5:#104281;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    color-scheme: dark;
    --page:#0d0d0d; --surface:#1a1a19;
    --ink:#ffffff; --ink-2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,0.10);
    --series-1:#3987e5;
    --track:#262624;
    --good:#0ca30c; --warning:#fab219;
    --ramp-1:#9ec5f4; --ramp-2:#6da7ec; --ramp-3:#3987e5;
    --ramp-4:#256abf; --ramp-5:#184f95;
  }
}
:root[data-theme="dark"]{
  color-scheme: dark;
  --page:#0d0d0d; --surface:#1a1a19;
  --ink:#ffffff; --ink-2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,0.10);
  --series-1:#3987e5;
  --track:#262624;
  --good:#0ca30c; --warning:#fab219;
  --ramp-1:#9ec5f4; --ramp-2:#6da7ec; --ramp-3:#3987e5;
  --ramp-4:#256abf; --ramp-5:#184f95;
}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);
  font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;}
.wrapper{max-width:1180px;margin:0 auto;padding:32px 24px 64px}
header{margin-bottom:8px}
h1{font-size:22px;font-weight:650;margin:0 0 4px}
.sub{color:var(--ink-2);margin:0 0 4px}
.meta{color:var(--muted);font-size:12px;margin:0}
h2{font-size:15px;font-weight:600;margin:0 0 2px}
.panel-note{color:var(--muted);font-size:12px;margin:0 0 14px}
section{background:var(--surface);border:1px solid var(--border);
  border-radius:10px;padding:18px 20px;margin-top:18px}
.grid{display:grid;gap:18px}
@media(min-width:860px){.grid-2{grid-template-columns:1fr 1fr}}
.tiles{display:grid;gap:12px;grid-template-columns:repeat(2,1fr);margin-top:18px}
@media(min-width:700px){.tiles{grid-template-columns:repeat(3,1fr)}}
@media(min-width:1000px){.tiles{grid-template-columns:repeat(auto-fit,minmax(148px,1fr))}}
.tile{background:var(--surface);border:1px solid var(--border);
  border-radius:10px;padding:14px 16px}
.tile .value{font-size:26px;font-weight:650;letter-spacing:-0.01em;
  line-height:1.15;color:var(--ink)}
.tile .label{color:var(--ink-2);font-size:12px;margin-top:4px}
.tile .foot{color:var(--muted);font-size:11px;margin-top:6px}
.tile.headline .value{color:var(--series-1)}
.bars{display:flex;flex-direction:column;gap:8px}
.bar-row{display:grid;grid-template-columns:minmax(120px,32%) 1fr 62px;
  align-items:center;gap:10px}
.bar-label{color:var(--ink-2);font-size:12px;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.bar-track{background:var(--track);border-radius:4px;height:14px;position:relative}
.bar-fill{height:14px;border-radius:0 4px 4px 0;background:var(--series-1)}
.bar-value{text-align:right;font-size:12px;color:var(--ink);
  font-variant-numeric:tabular-nums}
.histogram .bar-fill{border-radius:0 4px 4px 0}
.threshold{grid-column:1/-1;border-top:1px solid var(--warning);
  margin:4px 0 2px;padding-top:5px;color:var(--ink-2);font-size:11px}
.funnel{display:flex;flex-direction:column;gap:9px}
.funnel-row{display:grid;grid-template-columns:minmax(150px,38%) 1fr;
  align-items:center;gap:10px}
.funnel-label{color:var(--ink-2);font-size:12px}
.funnel-track{display:flex;align-items:center;gap:8px}
.funnel-fill{height:18px;border-radius:0 4px 4px 0}
.funnel-value{font-size:12px;color:var(--ink);font-variant-numeric:tabular-nums}
table{border-collapse:collapse;width:100%;font-size:12px;margin-top:6px}
caption{text-align:left;color:var(--muted);font-size:12px;padding-bottom:8px}
th{text-align:left;font-weight:600;color:var(--ink-2);
  border-bottom:1px solid var(--axis);padding:6px 8px;white-space:nowrap}
td{border-bottom:1px solid var(--grid);padding:6px 8px;
  font-variant-numeric:tabular-nums;vertical-align:top;white-space:nowrap}
/* System and target ids break on their hyphen and take two lines otherwise.
   The table already scrolls horizontally, so nothing is lost by holding them
   on one line. */
td.wrap{white-space:normal}
td.wrap{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;
  max-width:280px;max-height:64px;overflow:auto;display:block;
  color:var(--ink-2);font-variant-numeric:normal}
.scroll{overflow-x:auto}
.rules{display:flex;flex-wrap:wrap;gap:8px;margin-top:6px;padding:0;list-style:none}
.rules li{border:1px solid var(--border);border-radius:999px;
  padding:4px 11px;font-size:12px;color:var(--ink-2)}
.note{border-left:3px solid var(--warning);padding:8px 12px;margin-top:14px;
  color:var(--ink-2);font-size:12px;background:var(--page);border-radius:0 6px 6px 0}
.empty{color:var(--muted);font-size:12px;margin:4px 0}
footer{color:var(--muted);font-size:11px;margin-top:26px}
.bar-row.wide{grid-template-columns:minmax(110px,26%) 1fr 52px 92px}
.bar-value.muted{color:var(--muted)}
.stat-rules{display:flex;flex-wrap:wrap;gap:14px;margin-top:12px;
  padding-top:10px;border-top:1px solid var(--grid);color:var(--ink-2);
  font-size:12px;font-variant-numeric:tabular-nums}
.controls{display:flex;flex-wrap:wrap;align-items:center;gap:10px;
  padding-bottom:14px;margin-bottom:14px;border-bottom:1px solid var(--grid)}
.controls label{color:var(--ink-2);font-size:12px}
.controls input[type=range]{flex:1 1 260px;min-width:180px;accent-color:var(--series-1)}
.controls input[type=number]{width:88px;padding:5px 8px;font:inherit;
  font-variant-numeric:tabular-nums;color:var(--ink);background:var(--surface);
  border:1px solid var(--axis);border-radius:6px}
.controls .unit{color:var(--muted);font-size:12px;margin-left:-4px}
.controls button{padding:5px 11px;font:inherit;font-size:12px;color:var(--ink-2);
  background:var(--surface);border:1px solid var(--axis);border-radius:6px;
  cursor:pointer}
.controls button:hover{color:var(--ink);border-color:var(--ink-2)}
.scan-tiles{display:grid;gap:12px;grid-template-columns:repeat(2,1fr)}
@media(min-width:820px){.scan-tiles{grid-template-columns:repeat(4,1fr)}}
.scan-tiles .tile{background:var(--page)}
.scan-tiles .value{color:var(--series-1)}
.scan-table{margin-top:16px}
.scan-table tr.current td{font-weight:650;color:var(--ink)}
.scan-table tr.current td:first-child{box-shadow:inset 3px 0 0 var(--series-1)}
"""


def _share_note(metrics):
    """" — N% of it", when both lengths are measurable. "" otherwise."""
    total = metrics["gsep_length"].get("lower_pressure")
    candidate = metrics["length_stats"]["total"]
    if not total or candidate is None:
        return ""
    return f" — {100.0 * candidate / total:.1f}% of it"


def _table_caption(rows, total):
    """Say when the table is a slice, so a cap never reads as the whole list."""
    shown = len(rows)
    base = ("Nearest tie-in first. SOURCE_IDS traces each system back to the "
            "mains it was dissolved from.")
    if total and total > shown:
        return (f"The {shown:,} nearest of {total:,} candidates. {base} "
                f"The full set is in the GeoPackage.")
    return base

def render(metrics, table_headers, table_rows, source=None, total_candidates=None):
    """The dashboard as one self-contained HTML document."""
    generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    source_text = str(source or config.OUTPUT_GPKG)

    tiles = [
        ("headline", _count(metrics["candidates"]), "Insertion candidates",
         f"of {_count(metrics['examined'])} systems examined"),
        ("", _percent(metrics["conversion"]), "Conversion",
         "systems that qualified"),
        ("", _decimal(metrics["candidate_length_miles"], 1, " mi"),
         "Candidate main length",
         f"{_decimal(metrics['candidate_length_ft'], 0, ' ft')}"),
        ("", _miles(metrics["gsep_length"].get("lower_pressure")),
         "GSEP LPP main length",
         _feet(metrics["gsep_length"].get("lower_pressure"))),
        ("", _count(metrics["candidate_mains"]), "Source mains",
         "dissolved into the candidates"),
        ("", _decimal(metrics["median_distance_ft"], 1, " ft"),
         "Median tie-in distance", "candidates only"),
        ("", _count(metrics["crossing_systems"]), "Cross a CP boundary",
         "candidates spanning 2+ subnetworks"),
    ]
    tile_html = "".join(
        f'<div class="tile {css}"><div class="value">{escape(value)}</div>'
        f'<div class="label">{escape(label)}</div>'
        f'<div class="foot">{escape(foot)}</div></div>'
        for css, value, label, foot in tiles)

    rules = [
        f"Within {metrics['max_distance_ft']:g} ft",
        "Target pressure ≥ candidate, compared in PSI",
        f"Lower Pressure ≤ {metrics['lower_max_wc']:g}\" WC "
        f"or ≤ {metrics['lower_max_psi']:g} PSI",
        f"Other Pressure {metrics['other_min_psi']:g}–"
        f"{metrics['other_max_psi']:g} PSI",
        f"Cast iron ≤ {metrics['cast_iron_max_diameter']:g}\"",
        f"Coated steel before {metrics['coated_steel_cutoff']}",
        # Last, and not between the two GSEP material rules: these are not GSEP
        # rules. They are about whether the bore can take a carrier pipe.
        f"Insertable bore > {metrics['min_insertion_diameter_in']:g}\"",
        f"{metrics['min_insertion_diameter_in']:g}\" allowed above "
        f"{metrics['insertion_elevated_psi']:g} PSI",
    ]
    rule_html = "".join(f"<li>{escape(rule)}</li>" for rule in rules)

    at_minimum_note = ""
    if metrics["at_minimum_mains"]:
        at_minimum_note = (
            f'<p class="panel-note">'
            f'{metrics["at_minimum_mains"]:,} of those mains are at exactly '
            f'{metrics["min_insertion_diameter_in"]:g}", admitted because they '
            f'run above {metrics["insertion_elevated_psi"]:g} PSI. Anything '
            f'narrower is excluded at any pressure.</p>')

    plastic_note = ""
    if metrics["plastic_pending"]:
        plastic_note = (
            '<div class="note">Plastic ASSETTYPE values are not confirmed, so '
            'no plastic main counted as GSEP eligible in this run. The '
            'candidate count is a <strong>lower bound</strong> until '
            '<code>config.PLASTIC_ASSETTYPES</code> is filled in.</div>')

    no_target_note = ""
    if metrics["no_target"]:
        no_target_note = (
            f'<p class="panel-note">{_count(metrics["no_target"])} systems had '
            f'no Other Pressure system within the search limit at all, so they '
            f'have no distance and are not in these bins.</p>')

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>LPP GSEP Insertion Candidates</title>
<style>{STYLE}</style></head>
<body><div class="wrapper">

<header>
  <h1>LPP GSEP pipeline insertion candidates</h1>
  <p class="sub">GSEP-eligible Lower Pressure systems within
    {metrics['max_distance_ft']:g} ft of an Other Pressure system at or above
    their own pressure.</p>
  <p class="meta">Generated {escape(generated)} from {escape(source_text)}</p>
</header>

<div class="tiles">{tile_html}</div>

{plastic_note}

<section>
  <h2>What the run produced</h2>
  <p class="panel-note">Each stage counted independently — the target counts are
    a different population from the candidate counts, so these are not nested
    percentages of one another.</p>
  {_funnel(metrics["funnel"])}
  {at_minimum_note}
</section>

<div class="grid grid-2">
  <section>
    <h2>Why systems did not qualify</h2>
    <p class="panel-note">Every Lower Pressure system examined, by outcome. A
      candidate list that cannot explain its exclusions cannot be reviewed.</p>
    {_bar_rows(list(metrics["status"].items()))}
  </section>

  <section>
    <h2>Distance to the nearest target</h2>
    {no_target_note}
    {_histogram(metrics["distances"], metrics["max_distance_ft"])}
  </section>

  <section>
    <h2>What the candidates are made of</h2>
    <p class="panel-note">Counted per material, not per system: a system
      dissolved from mixed mains appears under each material it contains.</p>
    {_bar_rows(list(metrics["materials"].items()))}
  </section>

  <section>
    <h2>Oldest main in each candidate</h2>
    <p class="panel-note">By decade of the earliest installation date recorded
      on the system's mains.</p>
    {_bar_rows(list(metrics["decades"].items()))}
  </section>
</div>

<section>
  <h2>How long are the insertable candidates?</h2>
  <p class="panel-note">System count per bin, with the footage each bin holds.
    Mean and median are both given because they disagree: lengths are heavily
    right-skewed, so a few long runs pull the mean above the median and a
    single "average" would misdescribe most of the list.</p>
  {_length_bars(metrics["length_histogram"], metrics["length_stats"])}
  <p class="panel-note" style="margin-top:14px">
    {_feet(metrics["length_stats"]["total"])} of insertable main in
    {_count(metrics["length_stats"]["count"])} systems, out of
    {_feet(metrics["gsep_length"].get("lower_pressure"))} of GSEP-eligible
    Lower Pressure main{_share_note(metrics)}.</p>
</section>

<section>
  <h2>What if the distance threshold moved?</h2>
  <p class="panel-note">The only control on this page. It scopes this section
    only — everything above reports the run as it was configured, at
    {metrics['max_distance_ft']:g} ft.</p>
  {_distance_scan(metrics["distance_scan"], metrics["max_distance_ft"],
                  metrics["near_search_limit_ft"])}
</section>

<section>
  <h2>Cathodic protection</h2>
  <p class="panel-note">A candidate spanning more than one CP subnetwork is a
    constructability question before it is a scheduling one.</p>
  {_bar_rows(list(metrics["subnetworks"].items()))}
</section>

<section>
  <h2>Candidates</h2>
  <div class="scroll">
  {_table(table_headers, table_rows, _table_caption(table_rows, total_candidates))}
  </div>
</section>

<section>
  <h2>Rules this run applied</h2>
  <ul class="rules">{rule_html}</ul>
</section>

<footer>LPP GSEP Pipeline Insertion Evaluator — every threshold above is in
  <code>pipelineinsertion/config.py</code> and overridable by environment
  variable.</footer>

</div>
<script>{SCAN_SCRIPT}</script>
</body></html>
"""


def build(gpkg=None, target=None):
    """Read the GeoPackage and write the dashboard beside it. Returns the path."""
    step("Building the dashboard")
    gpkg = Path(gpkg or config.OUTPUT_GPKG)
    layers = read_layers(gpkg)
    metrics = dashboard_metrics.collect(layers)
    candidates = layers.get(schema.CANDIDATES_LAYER)
    headers, rows = dashboard_metrics.candidate_table(candidates)
    total = 0 if candidates is None else len(candidates)

    target = Path(target or dashboard_path(gpkg))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        render(metrics, headers, rows, source=gpkg, total_candidates=total),
        encoding="utf-8")

    log(f"  {metrics['candidates']:,} candidates from "
        f"{metrics['examined']:,} systems examined")
    log(f"Dashboard: {target}")
    return target
