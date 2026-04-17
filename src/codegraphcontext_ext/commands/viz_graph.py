"""cgc viz-graph: interactive force-directed graph of code structure.

Reads nodes and edges from KùzuDB, generates a standalone HTML file
with a vanilla-JS force-directed layout.  Color by node type, hover for
details, drag to rearrange.  No external dependencies — works offline.
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

COMMAND_NAME = "viz-graph"
SCHEMA_FILE = "context.json"  # reuse context schema stub for metadata
SUMMARY = "Interactive force-directed graph of code structure."

# Node tables to include.  Order matters for layering.
_NODE_TABLES = ("File", "Module", "Class", "Function", "Variable")

# Per-node identifier: File nodes carry no `uid` column upstream, so they are
# keyed by `.path`; Module nodes by `.name`; everything else by `.uid`.  The
# node fetch in `_fetch_graph` mirrors this precedence.  Edge queries must
# COALESCE across the same three columns so File/Module-backed edges resolve
# to the same identifier the corresponding node was registered under — without
# this, CONTAINS and INHERITS edges whose source or target is a File or Module
# silently drop (`src in nodes and dst in nodes` fails at line ~80).
_NODE_UID = "COALESCE(a.uid, a.path, a.name)"
_NODE_DST = "COALESCE(b.uid, b.path, b.name)"

# Relationship tables to include.
_REL_QUERIES = [
    ("CONTAINS", f"MATCH (a)-[r:CONTAINS]->(b) RETURN {_NODE_UID} AS src_uid, {_NODE_DST} AS dst_uid, 'CONTAINS' AS type"),
    ("CALLS", "MATCH (a)-[r:CALLS]->(b) RETURN a.uid AS src_uid, b.uid AS dst_uid, 'CALLS' AS type"),
    ("IMPORTS", "MATCH (a:File)-[r:IMPORTS]->(b:Module) RETURN a.path AS src_uid, b.name AS dst_uid, 'IMPORTS' AS type"),
    ("INHERITS", f"MATCH (a)-[r:INHERITS]->(b) RETURN {_NODE_UID} AS src_uid, {_NODE_DST} AS dst_uid, 'INHERITS' AS type"),
]


def _get_kuzu_connection() -> Any:
    from codegraphcontext.core.database_kuzu import KuzuDBManager
    manager = KuzuDBManager()
    driver = manager.get_driver()
    return driver.conn


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
            justify-content: space-between; align-items: center; }
  #header h1 { font-size: 18px; font-weight: 600; }
  #header .stats { font-size: 13px; color: #8b949e; }
  svg { width: 100vw; height: calc(100vh - 56px); }
  .link { stroke-opacity: 0.3; }
  .link-CALLS { stroke: #f0883e; }
  .link-CONTAINS { stroke: #30363d; }
  .link-IMPORTS { stroke: #58a6ff; }
  .link-INHERITS { stroke: #d2a8ff; }
  .node-label { font-size: 9px; fill: #8b949e; pointer-events: none; }
  .tooltip { position: absolute; background: #161b22; border: 1px solid #30363d;
             border-radius: 6px; padding: 10px 14px; font-size: 12px; pointer-events: none;
             max-width: 360px; box-shadow: 0 4px 12px rgba(0,0,0,0.4); display: none; }
  .tooltip .name { font-weight: 600; color: #58a6ff; margin-bottom: 4px; }
  .tooltip .path { color: #8b949e; }
  .legend { position: absolute; top: 72px; right: 24px; background: #161b22;
            border: 1px solid #30363d; border-radius: 6px; padding: 12px 16px; z-index: 10; }
  .legend-item { display: flex; align-items: center; gap: 8px; margin: 4px 0; font-size: 12px; }
  .legend-dot { width: 10px; height: 10px; border-radius: 50%; }
  .legend-line { width: 20px; height: 2px; }
  .legend-section { font-size: 10px; color: #8b949e; margin-top: 8px; text-transform: uppercase; letter-spacing: 0.5px; }
</style>
</head>
<body>
<div id="header">
  <h1>cgraph — Code Graph</h1>
  <div class="stats">__NODE_COUNT__ nodes &middot; __EDGE_COUNT__ edges</div>
</div>
<svg id="graph"></svg>
<div class="legend" id="legend">
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
</div>
<div class="tooltip" id="tooltip"></div>
<script>
const GRAPH = __GRAPH_JSON__;
const COLORS = { File: "#8b949e", Module: "#f778ba", Class: "#d2a8ff", Function: "#7ee787", Variable: "#79c0ff" };
const SIZES = { File: 8, Module: 6, Class: 7, Function: 5, Variable: 4 };
const EDGE_COLORS = { CONTAINS: "#30363d", CALLS: "#f0883e", IMPORTS: "#58a6ff", INHERITS: "#d2a8ff" };

const svg = document.getElementById("graph");
const W = window.innerWidth, H = window.innerHeight - 56;
svg.setAttribute("viewBox", "0 0 " + W + " " + H);

// State
const nodes = GRAPH.nodes.map(n => ({
  ...n, x: W/2 + (Math.random()-0.5)*W*0.4, y: H/2 + (Math.random()-0.5)*H*0.4, vx: 0, vy: 0,
  fx: null, fy: null
}));
const nodeMap = {};
nodes.forEach(n => nodeMap[n.id] = n);
const edges = GRAPH.edges.map(e => ({source: nodeMap[e.source], target: nodeMap[e.target], type: e.type}))
  .filter(e => e.source && e.target);

// SVG groups for zoom
const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
svg.appendChild(g);

// Create edge elements
const lineEls = edges.map(e => {
  const l = document.createElementNS("http://www.w3.org/2000/svg", "line");
  l.setAttribute("class", "link link-" + e.type);
  l.setAttribute("stroke", EDGE_COLORS[e.type] || "#30363d");
  l.setAttribute("stroke-width", "1");
  g.appendChild(l);
  return l;
});

// Create node elements
const circleEls = nodes.map((n, i) => {
  const c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
  c.setAttribute("r", SIZES[n.type] || 4);
  c.setAttribute("fill", COLORS[n.type] || "#8b949e");
  c.setAttribute("opacity", "0.85");
  c.setAttribute("cursor", "grab");
  c.dataset.idx = i;
  g.appendChild(c);
  return c;
});

// Create labels for Class, File, Module nodes
const labelNodes = nodes.filter(n => n.type === "Class" || n.type === "File" || n.type === "Module");
const labelEls = labelNodes.map(n => {
  const t = document.createElementNS("http://www.w3.org/2000/svg", "text");
  t.setAttribute("class", "node-label");
  t.setAttribute("dx", "10");
  t.setAttribute("dy", "3");
  t.textContent = n.name;
  g.appendChild(t);
  return t;
});

// Tooltip
const tooltip = document.getElementById("tooltip");
svg.addEventListener("mouseover", e => {
  if (e.target.tagName === "circle") {
    const n = nodes[e.target.dataset.idx];
    tooltip.textContent = '';
    const nd = document.createElement('div');
    nd.className = 'name';
    nd.appendChild(document.createTextNode(n.name + ' '));
    const ts = document.createElement('span');
    ts.style.cssText = 'color:#8b949e;font-weight:normal';
    ts.textContent = '(' + n.type + ')';
    nd.appendChild(ts);
    tooltip.appendChild(nd);
    if (n.path) {
      const pd = document.createElement('div');
      pd.className = 'path';
      pd.textContent = n.path + (n.line ? ':' + n.line : '');
      tooltip.appendChild(pd);
    }
    tooltip.style.display = "block";
  }
});
svg.addEventListener("mousemove", e => {
  tooltip.style.left = (e.pageX + 12) + "px";
  tooltip.style.top = (e.pageY - 12) + "px";
});
svg.addEventListener("mouseout", e => {
  if (e.target.tagName === "circle") tooltip.style.display = "none";
});

// Zoom + pan via wheel/pinch
let tx = 0, ty = 0, scale = 1;
function applyTransform() { g.setAttribute("transform", "translate("+tx+","+ty+") scale("+scale+")"); }
svg.addEventListener("wheel", e => {
  e.preventDefault();
  const factor = e.deltaY < 0 ? 1.1 : 0.9;
  const rect = svg.getBoundingClientRect();
  const mx = e.clientX - rect.left, my = e.clientY - rect.top;
  tx = mx - (mx - tx) * factor;
  ty = my - (my - ty) * factor;
  scale *= factor;
  applyTransform();
}, {passive: false});

// Pan via middle-click or shift+drag
let panning = false, panStartX, panStartY, panTx, panTy;
svg.addEventListener("mousedown", e => {
  if (e.button === 1 || (e.button === 0 && e.shiftKey)) {
    panning = true; panStartX = e.clientX; panStartY = e.clientY; panTx = tx; panTy = ty;
    e.preventDefault();
  }
});
window.addEventListener("mousemove", e => {
  if (panning) { tx = panTx + e.clientX - panStartX; ty = panTy + e.clientY - panStartY; applyTransform(); }
});
window.addEventListener("mouseup", () => { panning = false; });

// Drag nodes
let dragging = null;
svg.addEventListener("mousedown", e => {
  if (e.target.tagName === "circle" && !e.shiftKey && e.button === 0) {
    const idx = +e.target.dataset.idx;
    dragging = nodes[idx];
    dragging.fx = dragging.x; dragging.fy = dragging.y;
    e.preventDefault();
  }
});
window.addEventListener("mousemove", e => {
  if (dragging) {
    const rect = svg.getBoundingClientRect();
    dragging.fx = (e.clientX - rect.left - tx) / scale;
    dragging.fy = (e.clientY - rect.top - ty) / scale;
  }
});
window.addEventListener("mouseup", () => { if (dragging) { dragging.fx = null; dragging.fy = null; dragging = null; } });

// Force simulation
let alpha = 1;
function simulate() {
  if (alpha < 0.001) { requestAnimationFrame(simulate); return; }
  alpha *= 0.995;

  // Centering force
  nodes.forEach(n => { n.vx += (W/2 - n.x) * 0.001; n.vy += (H/2 - n.y) * 0.001; });

  // Charge repulsion (N^2 — fine for graphs under ~2000 nodes)
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      const dx = nodes[j].x - nodes[i].x, dy = nodes[j].y - nodes[i].y;
      const d2 = dx * dx + dy * dy + 1;
      const d = Math.sqrt(d2);
      const f = -150 * alpha / d2;
      const fx = (dx / d) * f, fy = (dy / d) * f;
      nodes[i].vx -= fx; nodes[i].vy -= fy;
      nodes[j].vx += fx; nodes[j].vy += fy;
    }
  }

  // Link spring force
  edges.forEach(e => {
    const dx = e.target.x - e.source.x, dy = e.target.y - e.source.y;
    const d = Math.sqrt(dx * dx + dy * dy) || 1;
    const f = (d - 60) * 0.03 * alpha;
    const fx = (dx / d) * f, fy = (dy / d) * f;
    e.source.vx += fx; e.source.vy += fy;
    e.target.vx -= fx; e.target.vy -= fy;
  });

  // Velocity damping + position update
  nodes.forEach(n => {
    if (n.fx != null) { n.x = n.fx; n.vx = 0; }
    else { n.vx *= 0.6; n.x += n.vx; }
    if (n.fy != null) { n.y = n.fy; n.vy = 0; }
    else { n.vy *= 0.6; n.y += n.vy; }
  });

  // Render
  for (let i = 0; i < edges.length; i++) {
    lineEls[i].setAttribute("x1", edges[i].source.x);
    lineEls[i].setAttribute("y1", edges[i].source.y);
    lineEls[i].setAttribute("x2", edges[i].target.x);
    lineEls[i].setAttribute("y2", edges[i].target.y);
  }
  for (let i = 0; i < nodes.length; i++) {
    circleEls[i].setAttribute("cx", nodes[i].x);
    circleEls[i].setAttribute("cy", nodes[i].y);
  }
  for (let i = 0; i < labelNodes.length; i++) {
    labelEls[i].setAttribute("x", labelNodes[i].x);
    labelEls[i].setAttribute("y", labelNodes[i].y);
  }

  requestAnimationFrame(simulate);
}

// Restart alpha on drag
svg.addEventListener("mousedown", e => { if (e.target.tagName === "circle") alpha = 0.3; });
simulate();
</script>
</body>
</html>"""


def _generate_html(graph: dict[str, Any]) -> str:
    safe_json = json.dumps(graph).replace("</", "<\\/")
    html = _HTML_TEMPLATE.replace("__GRAPH_JSON__", safe_json)
    html = html.replace("__NODE_COUNT__", str(len(graph["nodes"])))
    html = html.replace("__EDGE_COUNT__", str(len(graph["edges"])))
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
    no_open: bool = typer.Option(
        False,
        "--no-open",
        help="Write file but don't open in browser.",
    ),
) -> None:
    """Visualize code graph as an interactive force-directed diagram."""

    backend_payload = probe_backend_support()
    if not backend_payload["ok"]:
        typer.echo(emit_json(backend_payload))
        raise typer.Exit(code=1)

    conn = _get_kuzu_connection()
    print("Fetching graph data...", file=sys.stderr)
    graph = _fetch_graph(conn, limit=limit)

    if not graph["nodes"]:
        typer.echo(emit_json({
            "ok": False,
            "kind": "empty_graph",
            "detail": "No nodes found. Run `cgc index` first.",
        }))
        raise typer.Exit(code=1)

    html = _generate_html(graph)

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
        "output": os.path.abspath(out_path),
    }))
    raise typer.Exit(code=0)
