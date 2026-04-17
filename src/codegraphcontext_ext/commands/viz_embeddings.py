"""cgc viz-embeddings: interactive 2D scatter plot of code embeddings.

Reads embedding vectors from KùzuDB Function/Class nodes, reduces to 2D
via a numpy-only SVD-based PCA, and generates a standalone HTML file with
a vanilla-JS scatter plot.  Hover for details, color by node type.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import webbrowser
from typing import Any, Optional

import typer

from ..embeddings.schema import EMBEDDABLE_TABLES, EMBEDDING_COLUMN
from ..embeddings.runtime import probe_backend_support
from ..io.json_stdout import emit_json

COMMAND_NAME = "viz-embeddings"
SCHEMA_FILE = "context.json"  # reuse context schema stub for metadata
SUMMARY = "Interactive 2D scatter plot of code embedding vectors."


def _get_kuzu_connection() -> Any:
    from codegraphcontext.core.database_kuzu import KuzuDBManager
    manager = KuzuDBManager()
    driver = manager.get_driver()
    return driver.conn


def _fetch_embedded_nodes(conn: Any) -> list[dict[str, Any]]:
    """Fetch all nodes that have embedding vectors."""
    nodes: list[dict[str, Any]] = []
    for table in EMBEDDABLE_TABLES:
        query = (
            f"MATCH (n:`{table}`) "
            f"WHERE n.`{EMBEDDING_COLUMN}` IS NOT NULL "
            f"RETURN n.uid AS uid, n.name AS name, n.path AS path, "
            f"n.line_number AS line, n.`{EMBEDDING_COLUMN}` AS embedding"
        )
        try:
            result = conn.execute(query)
            while result.has_next():
                row = result.get_next()
                nodes.append({
                    "uid": row[0],
                    "name": row[1] or "(anonymous)",
                    "path": row[2] or "",
                    "line": row[3],
                    "embedding": list(row[4]),
                    "type": table,
                })
        except Exception:
            pass
    return nodes


def _reduce_to_2d(embeddings: list[list[float]]) -> list[list[float]]:
    """PCA reduction to 2D via numpy SVD (no sklearn dependency).

    Equivalent to sklearn.decomposition.PCA(n_components=2).fit_transform()
    for mean-centered input: projects onto the top 2 principal components
    derived from the SVD of the centered data matrix.
    """
    import numpy as np

    arr = np.asarray(embeddings, dtype=np.float64)
    if arr.ndim != 2 or arr.shape[0] < 2 or arr.shape[1] < 2:
        return [[0.0, 0.0] for _ in embeddings]

    centered = arr - arr.mean(axis=0)
    _, _, vt = np.linalg.svd(centered, full_matrices=False)
    reduced = centered @ vt[:2].T
    return reduced.tolist()


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>cgraph — Embedding Space</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #0d1117; color: #c9d1d9; }
  #header { padding: 16px 24px; border-bottom: 1px solid #30363d; display: flex;
            justify-content: space-between; align-items: center; }
  #header h1 { font-size: 18px; font-weight: 600; }
  #header .stats { font-size: 13px; color: #8b949e; }
  #chart { width: 100vw; height: calc(100vh - 56px); }
  svg { width: 100%; height: 100%; }
  .dot { cursor: pointer; transition: r 0.15s; }
  .dot:hover { r: 8; }
  .tooltip { position: absolute; background: #161b22; border: 1px solid #30363d;
             border-radius: 6px; padding: 10px 14px; font-size: 12px; pointer-events: none;
             max-width: 360px; box-shadow: 0 4px 12px rgba(0,0,0,0.4); }
  .tooltip .name { font-weight: 600; color: #58a6ff; margin-bottom: 4px; }
  .tooltip .path { color: #8b949e; }
  .tooltip .type { display: inline-block; padding: 1px 6px; border-radius: 3px;
                   font-size: 11px; margin-top: 4px; }
  .legend { position: absolute; top: 72px; right: 24px; background: #161b22;
            border: 1px solid #30363d; border-radius: 6px; padding: 12px 16px; }
  .legend-item { display: flex; align-items: center; gap: 8px; margin: 4px 0; font-size: 12px; }
  .legend-dot { width: 10px; height: 10px; border-radius: 50%; }
</style>
</head>
<body>
<div id="header">
  <h1>cgraph — Embedding Space (PCA 2D)</h1>
  <div class="stats">__NODE_COUNT__ nodes</div>
</div>
<div id="chart"></div>
<div class="legend" id="legend"></div>
<div class="tooltip" id="tooltip" style="display:none"></div>
<script>
const DATA = __DATA_JSON__;
const COLORS = { Function: "#7ee787", Class: "#d2a8ff", Variable: "#79c0ff", Other: "#8b949e" };

const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
document.getElementById("chart").appendChild(svg);
const tooltip = document.getElementById("tooltip");

const W = window.innerWidth, H = window.innerHeight - 56;
const xs = DATA.map(d => d.x), ys = DATA.map(d => d.y);
const pad = 60;
const xMin = Math.min(...xs), xMax = Math.max(...xs);
const yMin = Math.min(...ys), yMax = Math.max(...ys);
const xScale = v => pad + (v - xMin) / ((xMax - xMin) || 1) * (W - 2 * pad);
const yScale = v => pad + (v - yMin) / ((yMax - yMin) || 1) * (H - 2 * pad);

DATA.forEach(d => {
  const c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
  c.setAttribute("cx", xScale(d.x));
  c.setAttribute("cy", yScale(d.y));
  c.setAttribute("r", 5);
  c.setAttribute("fill", COLORS[d.type] || COLORS.Other);
  c.setAttribute("opacity", 0.8);
  c.classList.add("dot");
  c.addEventListener("mouseenter", e => {
    tooltip.textContent = '';
    const nd = document.createElement('div');
    nd.className = 'name';
    nd.textContent = d.name;
    tooltip.appendChild(nd);
    const pd = document.createElement('div');
    pd.className = 'path';
    pd.textContent = d.path + ':' + d.line;
    tooltip.appendChild(pd);
    const td = document.createElement('div');
    td.className = 'type';
    td.style.background = (COLORS[d.type]||COLORS.Other) + '33';
    td.style.color = COLORS[d.type]||COLORS.Other;
    td.textContent = d.type;
    tooltip.appendChild(td);
    tooltip.style.display = "block";
    tooltip.style.left = (e.pageX + 12) + "px";
    tooltip.style.top = (e.pageY - 12) + "px";
  });
  c.addEventListener("mousemove", e => {
    tooltip.style.left = (e.pageX + 12) + "px";
    tooltip.style.top = (e.pageY - 12) + "px";
  });
  c.addEventListener("mouseleave", () => { tooltip.style.display = "none"; });
  svg.appendChild(c);
});

// Legend
const legend = document.getElementById("legend");
Object.entries(COLORS).forEach(([type, color]) => {
  if (DATA.some(d => d.type === type)) {
    const item = document.createElement('div');
    item.className = 'legend-item';
    const dot = document.createElement('div');
    dot.className = 'legend-dot';
    dot.style.background = color;
    item.appendChild(dot);
    item.appendChild(document.createTextNode(type));
    legend.appendChild(item);
  }
});
</script>
</body>
</html>"""


def _generate_html(nodes: list[dict[str, Any]], points_2d: list[list[float]]) -> str:
    data = []
    for node, pt in zip(nodes, points_2d):
        data.append({
            "name": node["name"],
            "path": node["path"],
            "line": node["line"],
            "type": node["type"],
            "x": round(pt[0], 4),
            "y": round(pt[1], 4),
        })
    safe_json = json.dumps(data).replace("</", "<\\/")
    html = _HTML_TEMPLATE.replace("__DATA_JSON__", safe_json)
    html = html.replace("__NODE_COUNT__", str(len(data)))
    return html


def viz_embeddings_command(
    output: Optional[str] = typer.Option(
        None,
        "--output", "-o",
        help="Output HTML file path. Defaults to a temp file opened in browser.",
    ),
    no_open: bool = typer.Option(
        False,
        "--no-open",
        help="Write file but don't open in browser.",
    ),
) -> None:
    """Visualize code embeddings as an interactive 2D scatter plot."""

    backend_payload = probe_backend_support()
    if not backend_payload["ok"]:
        typer.echo(emit_json(backend_payload))
        raise typer.Exit(code=1)

    conn = _get_kuzu_connection()
    nodes = _fetch_embedded_nodes(conn)

    if not nodes:
        typer.echo(emit_json({
            "ok": False,
            "kind": "no_embeddings",
            "detail": "No embedded nodes found. Run `cgc embed` first.",
        }))
        raise typer.Exit(code=1)

    print(f"Reducing {len(nodes)} embeddings to 2D...", file=sys.stderr)
    embeddings = [n["embedding"] for n in nodes]
    points_2d = _reduce_to_2d(embeddings)

    html = _generate_html(nodes, points_2d)

    if output:
        out_path = output
    else:
        fd, out_path = tempfile.mkstemp(suffix=".html", prefix="cgraph-embeddings-")
        os.close(fd)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Wrote embedding visualization to {out_path}", file=sys.stderr)

    if not no_open:
        webbrowser.open(f"file://{os.path.abspath(out_path)}")

    typer.echo(emit_json({
        "ok": True,
        "kind": "viz_embeddings",
        "nodes": len(nodes),
        "output": os.path.abspath(out_path),
    }))
    raise typer.Exit(code=0)
