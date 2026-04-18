"""cgc viz-graph: interactive graph of code structure via Cytoscape.js.

Standalone HTML output; Cytoscape.js + cytoscape-dagre are loaded from unpkg
so opening the file requires a live internet connection.  The earlier vanilla
force-directed sim jittered visibly at 1-2K nodes — Cytoscape layouts compute
positions with `animate: false` and paint once.

Layouts (via --layout): cose (default, stabilized force-directed), dagre
(hierarchical top-down, good for File→Class→Function), concentric (rings
by node type), grid / breadthfirst / circle (fully deterministic).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import webbrowser
from typing import Any, Optional

import typer

from ..embeddings.runtime import probe_backend_support
from ..io.json_stdout import emit_json
from ..io.kuzu import get_kuzu_connection

COMMAND_NAME = "viz-graph"
SCHEMA_FILE = "context.json"  # reuse context schema stub for metadata
SUMMARY = "Interactive Cytoscape.js graph of code structure."

_LAYOUTS = ("cose", "dagre", "concentric", "grid", "breadthfirst", "circle")

# Node tables to include.  Order matters for layering.
_NODE_TABLES = ("File", "Module", "Class", "Function", "Variable")

# Per-node identifier: File nodes carry no `uid` column upstream, so they are
# keyed by `.path`; Module nodes by `.name`; everything else by `.uid`.  The
# node fetch in `_fetch_graph` mirrors this precedence.  Edge queries must
# COALESCE across the same three columns so File/Module-backed edges resolve
# to the same identifier the corresponding node was registered under — without
# this, CONTAINS and INHERITS edges whose source or target is a File or Module
# silently drop (`src in nodes and dst in nodes` fails downstream).
_NODE_UID = "COALESCE(a.uid, a.path, a.name)"
_NODE_DST = "COALESCE(b.uid, b.path, b.name)"

_REL_QUERIES = [
    ("CONTAINS", f"MATCH (a)-[r:CONTAINS]->(b) RETURN {_NODE_UID} AS src_uid, {_NODE_DST} AS dst_uid, 'CONTAINS' AS type"),
    ("CALLS", "MATCH (a)-[r:CALLS]->(b) RETURN a.uid AS src_uid, b.uid AS dst_uid, 'CALLS' AS type"),
    ("IMPORTS", "MATCH (a:File)-[r:IMPORTS]->(b:Module) RETURN a.path AS src_uid, b.name AS dst_uid, 'IMPORTS' AS type"),
    ("INHERITS", f"MATCH (a)-[r:INHERITS]->(b) RETURN {_NODE_UID} AS src_uid, {_NODE_DST} AS dst_uid, 'INHERITS' AS type"),
]


def _fetch_graph(conn: Any, *, limit: int) -> dict[str, Any]:
    """Fetch nodes and edges from KùzuDB."""
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []

    for table in _NODE_TABLES:
        try:
            if table == "File":
                q = f"MATCH (n:`{table}`) RETURN n.path AS uid, n.name AS name, n.path AS path, 0 AS line LIMIT {limit}"
            elif table == "Module":
                q = f"MATCH (n:`{table}`) RETURN n.name AS uid, n.name AS name, '' AS path, 0 AS line LIMIT {limit}"
            else:
                q = f"MATCH (n:`{table}`) RETURN n.uid AS uid, n.name AS name, n.path AS path, n.line_number AS line LIMIT {limit}"
            result = conn.execute(q)
            while result.has_next():
                row = result.get_next()
                uid = row[0]
                if uid and uid not in nodes:
                    nodes[uid] = {
                        "id": uid,
                        "name": row[1] or "(anonymous)",
                        "path": row[2] or "",
                        "line": row[3] or 0,
                        "type": table,
                    }
        except Exception:
            pass

    for rel_name, query in _REL_QUERIES:
        try:
            result = conn.execute(query + f" LIMIT {limit}")
            while result.has_next():
                row = result.get_next()
                src, dst, rtype = row[0], row[1], row[2]
                if src and dst and src in nodes and dst in nodes:
                    edges.append({"source": src, "target": dst, "type": rtype})
        except Exception:
            pass

    return {"nodes": list(nodes.values()), "edges": edges}


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>cgraph — Code Graph</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #0d1117; color: #c9d1d9; overflow: hidden; }
  #header { padding: 16px 24px; border-bottom: 1px solid #30363d; display: flex;
            justify-content: space-between; align-items: center; gap: 16px; }
  #header h1 { font-size: 18px; font-weight: 600; white-space: nowrap; }
  #header .stats { font-size: 13px; color: #8b949e; white-space: nowrap; }
  #header .controls { display: flex; align-items: center; gap: 12px; margin-left: auto; }
  #header select, #header input { background: #0d1117; color: #c9d1d9; border: 1px solid #30363d;
                                   border-radius: 6px; padding: 6px 10px; font-size: 12px; }
  #header input { width: 220px; }
  #cy { width: 100vw; height: calc(100vh - 57px); background: #0d1117; }
  .tooltip { position: absolute; background: #161b22; border: 1px solid #30363d;
             border-radius: 6px; padding: 10px 14px; font-size: 12px; pointer-events: none;
             max-width: 360px; box-shadow: 0 4px 12px rgba(0,0,0,0.4); display: none; z-index: 20; }
  .tooltip .name { font-weight: 600; color: #58a6ff; margin-bottom: 4px; }
  .tooltip .path { color: #8b949e; }
  .legend { position: absolute; top: 72px; right: 24px; background: #161b22;
            border: 1px solid #30363d; border-radius: 6px; padding: 12px 16px; z-index: 10; }
  .legend-item { display: flex; align-items: center; gap: 8px; margin: 4px 0; font-size: 12px; }
  .legend-dot { width: 10px; height: 10px; border-radius: 50%; }
  .legend-line { width: 20px; height: 2px; }
  .legend-section { font-size: 10px; color: #8b949e; margin-top: 8px;
                    text-transform: uppercase; letter-spacing: 0.5px; }
</style>
</head>
<body>
<div id="header">
  <h1>cgraph — Code Graph</h1>
  <div class="stats">__NODE_COUNT__ nodes &middot; __EDGE_COUNT__ edges &middot; layout: <span id="layout-label">__LAYOUT_NAME__</span></div>
  <div class="controls">
    <select id="layout-select">
      <option value="cose">cose</option>
      <option value="dagre">dagre</option>
      <option value="concentric">concentric</option>
      <option value="grid">grid</option>
      <option value="breadthfirst">breadthfirst</option>
      <option value="circle">circle</option>
    </select>
    <input id="search" type="text" placeholder="Search node name...">
  </div>
</div>
<div id="cy"></div>
<div class="legend">
  <div class="legend-section">Nodes</div>
  <div class="legend-item"><div class="legend-dot" style="background:#8b949e"></div>File</div>
  <div class="legend-item"><div class="legend-dot" style="background:#f778ba"></div>Module</div>
  <div class="legend-item"><div class="legend-dot" style="background:#d2a8ff"></div>Class</div>
  <div class="legend-item"><div class="legend-dot" style="background:#7ee787"></div>Function</div>
  <div class="legend-item"><div class="legend-dot" style="background:#79c0ff"></div>Variable</div>
  <div class="legend-section" style="margin-top:12px">Edges</div>
  <div class="legend-item"><div class="legend-line" style="background:#30363d"></div>Contains</div>
  <div class="legend-item"><div class="legend-line" style="background:#f0883e"></div>Calls</div>
  <div class="legend-item"><div class="legend-line" style="background:#58a6ff"></div>Imports</div>
  <div class="legend-item"><div class="legend-line" style="background:#d2a8ff"></div>Inherits</div>
  <div class="legend-section" style="margin-top:12px">Interactions</div>
  <div class="legend-item" style="color:#8b949e">Tap: highlight neighborhood</div>
  <div class="legend-item" style="color:#8b949e">Scroll: zoom &middot; Drag: pan</div>
</div>
<div class="tooltip" id="tooltip"></div>
<script src="https://unpkg.com/cytoscape@3.28.1/dist/cytoscape.min.js"></script>
<script src="https://unpkg.com/dagre@0.8.5/dist/dagre.min.js"></script>
<script src="https://unpkg.com/cytoscape-dagre@2.5.0/cytoscape-dagre.js"></script>
<script>
const GRAPH = __GRAPH_JSON__;
const INITIAL_LAYOUT = "__LAYOUT_NAME__";
const COLORS = { File: "#8b949e", Module: "#f778ba", Class: "#d2a8ff", Function: "#7ee787", Variable: "#79c0ff" };
const EDGE_COLORS = { CONTAINS: "#30363d", CALLS: "#f0883e", IMPORTS: "#58a6ff", INHERITS: "#d2a8ff" };
const SIZES = { File: 18, Module: 14, Class: 16, Function: 10, Variable: 8 };
const TYPE_RANK = { File: 5, Module: 4, Class: 3, Function: 2, Variable: 1 };

if (typeof cytoscapeDagre !== "undefined") cytoscape.use(cytoscapeDagre);

const elements = [
  ...GRAPH.nodes.map(n => ({
    data: { id: n.id, label: n.name, type: n.type, path: n.path, line: n.line },
  })),
  ...GRAPH.edges.map((e, i) => ({
    data: { id: "e" + i, source: e.source, target: e.target, type: e.type },
  })),
];

// animate:false — layout computes positions and paints once, no jitter.
const LAYOUT_CONFIGS = {
  cose:         { name: "cose", animate: false, nodeRepulsion: 8000, idealEdgeLength: 70, nodeOverlap: 12, gravity: 0.25, numIter: 1500 },
  dagre:        { name: "dagre", rankDir: "TB", animate: false, spacingFactor: 1.1, nodeDimensionsIncludeLabels: true },
  concentric:   { name: "concentric", animate: false, concentric: n => TYPE_RANK[n.data("type")] || 0, levelWidth: () => 1, minNodeSpacing: 30 },
  grid:         { name: "grid", animate: false, avoidOverlap: true, condense: false },
  breadthfirst: { name: "breadthfirst", animate: false, directed: true, spacingFactor: 1.1 },
  circle:       { name: "circle", animate: false },
};

const cy = cytoscape({
  container: document.getElementById("cy"),
  elements: elements,
  wheelSensitivity: 0.25,
  style: [
    { selector: "node", style: {
        "background-color": ele => COLORS[ele.data("type")] || "#8b949e",
        "width": ele => SIZES[ele.data("type")] || 8,
        "height": ele => SIZES[ele.data("type")] || 8,
        "label": ele => ["File","Module","Class"].includes(ele.data("type")) ? ele.data("label") : "",
        "color": "#8b949e",
        "font-size": 9,
        "text-halign": "right",
        "text-valign": "center",
        "text-margin-x": 4,
        "text-wrap": "none",
        "opacity": 0.9,
    }},
    { selector: "edge", style: {
        "line-color": ele => EDGE_COLORS[ele.data("type")] || "#30363d",
        "width": 1,
        "curve-style": "straight",
        "target-arrow-shape": "triangle",
        "target-arrow-color": ele => EDGE_COLORS[ele.data("type")] || "#30363d",
        "arrow-scale": 0.5,
        "opacity": 0.35,
    }},
    { selector: ".faded", style: { "opacity": 0.08 } },
    { selector: ".hit",   style: { "opacity": 1, "z-index": 10 } },
    { selector: "node.hit", style: { "border-width": 2, "border-color": "#58a6ff" } },
  ],
  layout: LAYOUT_CONFIGS[INITIAL_LAYOUT] || LAYOUT_CONFIGS.cose,
});

// Tooltip on hover
const tooltip = document.getElementById("tooltip");
cy.on("mouseover", "node", e => {
  const n = e.target.data();
  tooltip.textContent = "";
  const nd = document.createElement("div");
  nd.className = "name";
  nd.appendChild(document.createTextNode(n.label + " "));
  const ts = document.createElement("span");
  ts.style.cssText = "color:#8b949e;font-weight:normal";
  ts.textContent = "(" + n.type + ")";
  nd.appendChild(ts);
  tooltip.appendChild(nd);
  if (n.path) {
    const pd = document.createElement("div");
    pd.className = "path";
    pd.textContent = n.path + (n.line ? ":" + n.line : "");
    tooltip.appendChild(pd);
  }
  tooltip.style.display = "block";
});
cy.on("mousemove", e => {
  const ev = e.originalEvent;
  if (ev) {
    tooltip.style.left = (ev.pageX + 12) + "px";
    tooltip.style.top = (ev.pageY - 12) + "px";
  }
});
cy.on("mouseout", "node", () => { tooltip.style.display = "none"; });

// Tap to highlight closed neighborhood; tap background to clear.
function clearHighlight() { cy.elements().removeClass("faded hit"); }
cy.on("tap", "node", e => {
  clearHighlight();
  cy.elements().addClass("faded");
  e.target.closedNeighborhood().removeClass("faded").addClass("hit");
});
cy.on("tap", e => { if (e.target === cy) clearHighlight(); });

// Layout switcher
const layoutSelect = document.getElementById("layout-select");
const layoutLabel = document.getElementById("layout-label");
layoutSelect.value = INITIAL_LAYOUT;
layoutSelect.addEventListener("change", () => {
  const name = layoutSelect.value;
  layoutLabel.textContent = name;
  cy.layout(LAYOUT_CONFIGS[name] || LAYOUT_CONFIGS.cose).run();
});

// Search: exact-prefix highlight, Enter fits to matches.
const search = document.getElementById("search");
search.addEventListener("input", () => {
  const q = search.value.trim().toLowerCase();
  clearHighlight();
  if (!q) return;
  const matches = cy.nodes().filter(n => (n.data("label") || "").toLowerCase().includes(q));
  if (matches.length === 0) return;
  cy.elements().addClass("faded");
  matches.union(matches.connectedEdges()).union(matches.openNeighborhood()).removeClass("faded").addClass("hit");
});
search.addEventListener("keydown", e => {
  if (e.key === "Enter") {
    const hits = cy.$(".hit");
    if (hits.length > 0) cy.fit(hits, 40);
  }
});
</script>
</body>
</html>"""


_HTML_TEMPLATE_3D = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>cgraph — Code Graph (3D)</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #0d1117; color: #c9d1d9; overflow: hidden; }
  #header { position: absolute; top: 0; left: 0; right: 0; z-index: 20;
            padding: 16px 24px; border-bottom: 1px solid #30363d;
            background: rgba(13,17,23,0.85); backdrop-filter: blur(6px);
            display: flex; justify-content: space-between; align-items: center; gap: 16px; }
  #header h1 { font-size: 18px; font-weight: 600; }
  #header .stats { font-size: 13px; color: #8b949e; }
  #header .controls { display: flex; align-items: center; gap: 12px; margin-left: auto; }
  #header input { background: #0d1117; color: #c9d1d9; border: 1px solid #30363d;
                   border-radius: 6px; padding: 6px 10px; font-size: 12px; width: 220px; }
  #graph { width: 100vw; height: 100vh; }
  .tooltip { position: absolute; background: #161b22; border: 1px solid #30363d;
             border-radius: 6px; padding: 10px 14px; font-size: 12px; pointer-events: none;
             max-width: 360px; box-shadow: 0 4px 12px rgba(0,0,0,0.4); display: none; z-index: 30; }
  .tooltip .name { font-weight: 600; color: #58a6ff; margin-bottom: 4px; }
  .tooltip .path { color: #8b949e; }
  .legend { position: absolute; top: 72px; right: 24px; background: #161b22;
            border: 1px solid #30363d; border-radius: 6px; padding: 12px 16px; z-index: 10; }
  .legend-item { display: flex; align-items: center; gap: 8px; margin: 4px 0; font-size: 12px; }
  .legend-dot { width: 10px; height: 10px; border-radius: 50%; }
  .legend-line { width: 20px; height: 2px; }
  .legend-section { font-size: 10px; color: #8b949e; margin-top: 8px;
                    text-transform: uppercase; letter-spacing: 0.5px; }
</style>
</head>
<body>
<div id="header">
  <h1>cgraph — Code Graph (3D)</h1>
  <div class="stats">__NODE_COUNT__ nodes &middot; __EDGE_COUNT__ edges &middot; drag to rotate</div>
  <div class="controls">
    <input id="search" type="text" placeholder="Search node name...">
  </div>
</div>
<div id="graph"></div>
<div class="legend">
  <div class="legend-section">Nodes</div>
  <div class="legend-item"><div class="legend-dot" style="background:#8b949e"></div>File</div>
  <div class="legend-item"><div class="legend-dot" style="background:#f778ba"></div>Module</div>
  <div class="legend-item"><div class="legend-dot" style="background:#d2a8ff"></div>Class</div>
  <div class="legend-item"><div class="legend-dot" style="background:#7ee787"></div>Function</div>
  <div class="legend-item"><div class="legend-dot" style="background:#79c0ff"></div>Variable</div>
  <div class="legend-section" style="margin-top:12px">Edges</div>
  <div class="legend-item"><div class="legend-line" style="background:#30363d"></div>Contains</div>
  <div class="legend-item"><div class="legend-line" style="background:#f0883e"></div>Calls</div>
  <div class="legend-item"><div class="legend-line" style="background:#58a6ff"></div>Imports</div>
  <div class="legend-item"><div class="legend-line" style="background:#d2a8ff"></div>Inherits</div>
  <div class="legend-section" style="margin-top:12px">Interactions</div>
  <div class="legend-item" style="color:#8b949e">Drag: rotate &middot; Scroll: zoom</div>
  <div class="legend-item" style="color:#8b949e">Right-drag: pan &middot; Click: focus</div>
</div>
<div class="tooltip" id="tooltip"></div>
<script src="https://unpkg.com/3d-force-graph@1.73.4/dist/3d-force-graph.min.js"></script>
<script>
const GRAPH = __GRAPH_JSON__;
const COLORS = { File: "#8b949e", Module: "#f778ba", Class: "#d2a8ff", Function: "#7ee787", Variable: "#79c0ff" };
const EDGE_COLORS = { CONTAINS: "#30363d", CALLS: "#f0883e", IMPORTS: "#58a6ff", INHERITS: "#d2a8ff" };
const SIZES = { File: 6, Module: 5, Class: 5, Function: 3.5, Variable: 2.5 };

const graphData = {
  nodes: GRAPH.nodes.map(n => ({
    id: n.id, name: n.name, type: n.type, path: n.path, line: n.line,
  })),
  links: GRAPH.edges.map(e => ({ source: e.source, target: e.target, type: e.type })),
};

const tooltip = document.getElementById("tooltip");
function showTooltip(n, event) {
  tooltip.textContent = "";
  const nd = document.createElement("div");
  nd.className = "name";
  nd.appendChild(document.createTextNode(n.name + " "));
  const ts = document.createElement("span");
  ts.style.cssText = "color:#8b949e;font-weight:normal";
  ts.textContent = "(" + n.type + ")";
  nd.appendChild(ts);
  tooltip.appendChild(nd);
  if (n.path) {
    const pd = document.createElement("div");
    pd.className = "path";
    pd.textContent = n.path + (n.line ? ":" + n.line : "");
    tooltip.appendChild(pd);
  }
  tooltip.style.left = (event.pageX + 12) + "px";
  tooltip.style.top = (event.pageY - 12) + "px";
  tooltip.style.display = "block";
}
document.getElementById("graph").addEventListener("mousemove", e => {
  if (tooltip.style.display === "block") {
    tooltip.style.left = (e.pageX + 12) + "px";
    tooltip.style.top = (e.pageY - 12) + "px";
  }
});

// cooldownTicks=120 / d3AlphaDecay=0.05 — sim settles in ~1.5s then hard-stops.
// Not the never-converging pathology the old vanilla sim had.
const Graph = ForceGraph3D()(document.getElementById("graph"))
  .graphData(graphData)
  .backgroundColor("#0d1117")
  .nodeRelSize(4)
  .nodeVal(n => SIZES[n.type] || 3)
  .nodeColor(n => COLORS[n.type] || "#8b949e")
  .nodeLabel(n => n.name + " (" + n.type + ")")
  .linkColor(l => EDGE_COLORS[l.type] || "#30363d")
  .linkOpacity(0.35)
  .linkDirectionalArrowLength(3)
  .linkDirectionalArrowRelPos(1)
  .cooldownTicks(120)
  .d3AlphaDecay(0.05)
  .warmupTicks(0)
  .onNodeHover(n => {
    document.getElementById("graph").style.cursor = n ? "pointer" : null;
    if (!n) { tooltip.style.display = "none"; return; }
    // 3d-force-graph doesn't pass the pointer event — synthesize from last mousemove.
    const evt = window._lastMove || { pageX: window.innerWidth / 2, pageY: window.innerHeight / 2 };
    showTooltip(n, evt);
  })
  .onNodeClick(n => {
    // Zoom camera to focus on the clicked node.
    const distance = 80;
    const distRatio = 1 + distance / Math.hypot(n.x || 1, n.y || 1, n.z || 1);
    Graph.cameraPosition(
      { x: (n.x || 0) * distRatio, y: (n.y || 0) * distRatio, z: (n.z || 0) * distRatio },
      n,
      1500,
    );
  });

document.addEventListener("mousemove", e => { window._lastMove = e; });

// Search: filter nodes by name; dim everything else.
const search = document.getElementById("search");
search.addEventListener("input", () => {
  const q = search.value.trim().toLowerCase();
  Graph
    .nodeOpacity(q ? (n => (n.name || "").toLowerCase().includes(q) ? 1 : 0.1) : 0.85)
    .linkOpacity(q ? 0.05 : 0.35);
});
</script>
</body>
</html>"""


def _generate_html(graph: dict[str, Any], *, layout: str = "cose", three_d: bool = False) -> str:
    safe_json = json.dumps(graph).replace("</", "<\\/")
    template = _HTML_TEMPLATE_3D if three_d else _HTML_TEMPLATE
    html = template.replace("__GRAPH_JSON__", safe_json)
    html = html.replace("__NODE_COUNT__", str(len(graph["nodes"])))
    html = html.replace("__EDGE_COUNT__", str(len(graph["edges"])))
    html = html.replace("__LAYOUT_NAME__", layout)
    return html


def viz_graph_command(
    output: Optional[str] = typer.Option(
        None,
        "--output", "-o",
        help="Output HTML file path. Defaults to a temp file opened in browser.",
    ),
    limit: int = typer.Option(
        500,
        "--limit",
        help="Max nodes per table to fetch.",
    ),
    layout: str = typer.Option(
        "cose",
        "--layout",
        help=f"2D initial layout ({', '.join(_LAYOUTS)}). Ignored when --3d is set.",
    ),
    three_d: bool = typer.Option(
        False,
        "--3d/--2d",
        help="Render in 3D via 3d-force-graph (drag-rotate, scroll-zoom). Default is 2D Cytoscape.",
    ),
    no_open: bool = typer.Option(
        False,
        "--no-open",
        help="Write file but don't open in browser.",
    ),
) -> None:
    """Visualize code graph as an interactive diagram (2D Cytoscape or 3D force-directed)."""

    if layout not in _LAYOUTS:
        raise typer.BadParameter(
            f"unknown layout {layout!r}; expected one of {sorted(_LAYOUTS)}"
        )

    backend_payload = probe_backend_support()
    if not backend_payload["ok"]:
        typer.echo(emit_json(backend_payload))
        raise typer.Exit(code=1)

    conn = get_kuzu_connection()
    print("Fetching graph data...", file=sys.stderr)
    graph = _fetch_graph(conn, limit=limit)

    if not graph["nodes"]:
        typer.echo(emit_json({
            "ok": False,
            "kind": "empty_graph",
            "detail": "No nodes found. Run `cgc index` first.",
        }))
        raise typer.Exit(code=1)

    html = _generate_html(graph, layout=layout, three_d=three_d)

    if output:
        out_path = output
    else:
        fd, out_path = tempfile.mkstemp(suffix=".html", prefix="cgraph-graph-")
        os.close(fd)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Wrote graph visualization to {out_path}", file=sys.stderr)

    if not no_open:
        webbrowser.open(f"file://{os.path.abspath(out_path)}")

    typer.echo(emit_json({
        "ok": True,
        "kind": "viz_graph",
        "nodes": len(graph["nodes"]),
        "edges": len(graph["edges"]),
        "layout": layout,
        "mode": "3d" if three_d else "2d",
        "output": os.path.abspath(out_path),
    }))
    raise typer.Exit(code=0)
