"""cgc viz-dashboard: server-backed 3-tab viz dashboard.

Three tabs in one browser window:
  1. 2D Graph   — Cytoscape.js (srcdoc iframe)
  2. 3D Graph   — 3d-force-graph (srcdoc iframe)
  3. Embeddings — TF Embedding Projector (iframe src="projector/")

The Projector can't be srcdoc-inlined — its JS does real fetch() calls for
projector_config.json and the TSVs, which fail from a srcdoc iframe's
opaque origin.  So the dashboard runs as a real HTTP server rooted on a
tempdir; the dashboard HTML and the Projector live side-by-side in it.

Blocks until Ctrl-C; cleans up the tempdir on exit.
"""

from __future__ import annotations

import html as _html
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

import typer

from ..embeddings.fetch import fetch_embedded_nodes
from ..embeddings.runtime import probe_backend_support
from ..io.json_stdout import emit_json
from ..io.kuzu import get_kuzu_connection
from ..viz_server import (
    build_server,
    copy_vendored_projector,
    find_free_port,
    serve_until_interrupted,
    write_projector_data,
)
from .viz_graph import (
    _LAYOUTS,
    _fetch_graph,
    _generate_html as _generate_graph_html,
)

COMMAND_NAME = "viz-dashboard"
SCHEMA_FILE = "context.json"
SUMMARY = "Unified dashboard: 2D graph, 3D graph, embeddings scatter, and TF Projector as tabs."


_DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>cgraph — Dashboard</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #0d1117; color: #c9d1d9; overflow: hidden; height: 100vh; display: flex; flex-direction: column; }
  #nav { padding: 12px 24px; border-bottom: 1px solid #30363d; display: flex; align-items: center; gap: 24px; flex-shrink: 0; }
  #nav h1 { font-size: 16px; font-weight: 600; color: #c9d1d9; }
  #nav .stats { font-size: 12px; color: #8b949e; margin-right: auto; }
  .tab-bar { display: flex; gap: 4px; }
  .tab { padding: 8px 16px; font-size: 13px; color: #8b949e; background: transparent;
         border: 1px solid transparent; border-radius: 6px; cursor: pointer; font-family: inherit;
         transition: background 0.12s; }
  .tab:hover { background: #161b22; color: #c9d1d9; }
  .tab.active { background: #161b22; color: #58a6ff; border-color: #30363d; }
  #panes { flex: 1; position: relative; }
  /* Stack every pane at full size; hide inactive via opacity so iframes init at
     real dimensions and keep running RAF.  display:none makes 3d-force-graph
     and the SVG scatter measure 0 at load and never recover. */
  .pane { position: absolute; inset: 0; opacity: 0; pointer-events: none; }
  .pane.active { opacity: 1; pointer-events: auto; }
  .pane iframe { width: 100%; height: 100%; border: 0; background: #0d1117; }

  /* Embeddings pane: cgraph-styled explainer bar above the Projector iframe. */
  .embeddings-wrap { display: flex; flex-direction: column; width: 100%; height: 100%; }
  .embeddings-header { flex-shrink: 0; padding: 12px 24px; background: #161b22;
                       border-bottom: 1px solid #30363d; display: flex; align-items: center; gap: 16px; }
  .embeddings-header .lede { flex: 1; font-size: 13px; color: #c9d1d9; line-height: 1.5; }
  .embeddings-header .lede strong { color: #58a6ff; }
  .embeddings-header .lede .hint { color: #8b949e; }
  .embeddings-header .mode-toggle { display: flex; gap: 4px; flex-shrink: 0; }
  .embeddings-header .mode-toggle button {
    padding: 6px 12px; font-size: 12px; color: #8b949e; background: transparent;
    border: 1px solid #30363d; border-radius: 6px; cursor: pointer; font-family: inherit;
  }
  .embeddings-header .mode-toggle button:hover { color: #c9d1d9; background: #1c2029; }
  .embeddings-header .mode-toggle button.active { color: #58a6ff; border-color: #58a6ff; background: #1c2029; }
  .embeddings-wrap iframe { flex: 1; min-height: 0; }
</style>
</head>
<body>
<div id="nav">
  <h1>cgraph</h1>
  <div class="stats">__NODE_COUNT__ nodes &middot; __EDGE_COUNT__ edges &middot; __EMB_COUNT__ embeddings</div>
  <div class="tab-bar" id="tab-bar">
    <button class="tab active" data-pane="pane-2d">2D Graph</button>
    <button class="tab" data-pane="pane-3d">3D Graph</button>
    <button class="tab" data-pane="pane-embeddings">Embeddings</button>
  </div>
</div>
<div id="panes">
  <!-- 2D pane is visible on load; srcdoc set immediately.  3D pane is
       hidden on load and Chrome refuses WebGL for invisible iframes — so
       its srcdoc is stashed as data-srcdoc and promoted on first tab
       click by the JS below, where the iframe is actually visible. -->
  <div class="pane active" id="pane-2d">
    <iframe id="iframe-2d" srcdoc="__IFRAME_2D__"></iframe>
  </div>
  <div class="pane" id="pane-3d">
    <iframe id="iframe-3d" data-srcdoc="__IFRAME_3D__"></iframe>
  </div>
  <div class="pane" id="pane-embeddings">
    <div class="embeddings-wrap">
      <div class="embeddings-header">
        <div class="lede">
          <strong>Each dot is a function.</strong> Functions that do similar things cluster together — even when the names don&rsquo;t match.
          <span class="hint">Click a dot to see its semantic neighbors on the right. Use the <em>UMAP</em> tab (bottom-left) for the clearest clusters.</span>
        </div>
        <div class="mode-toggle">
          <button type="button" id="emb-simple-btn" class="active">Simple</button>
          <button type="button" id="emb-advanced-btn">Advanced</button>
        </div>
      </div>
      <!-- src set lazily on first Embeddings-tab click (see bottom of page).
           Reason: the pane starts at opacity:0, and Chrome refuses to hand a
           WebGL context to an invisible iframe.  If we set src up front, the
           Projector's one-shot hasWebGLSupport() check runs before the user
           ever sees the tab, caches "no WebGL", and shows its error dialog
           forever.  Setting src on first click = WebGL init happens while
           the iframe is visible. -->
      <iframe id="emb-iframe"></iframe>
    </div>
  </div>
</div>
<script>
const tabs = document.querySelectorAll(".tab");
const panes = document.querySelectorAll(".pane");

// Projector pane state.
const simpleBtn = document.getElementById("emb-simple-btn");
const advancedBtn = document.getElementById("emb-advanced-btn");
const embIframe = document.getElementById("emb-iframe");
let embLoaded = false;
let embAdvanced = false;

function loadEmbIframe() {
  embIframe.src = embAdvanced ? "projector/?advanced=1" : "projector/";
  embLoaded = true;
}

function setEmbMode(advanced) {
  embAdvanced = advanced;
  simpleBtn.classList.toggle("active", !advanced);
  advancedBtn.classList.toggle("active", advanced);
  // Re-set src so the Projector re-runs its init with/without ?advanced=1.
  if (embLoaded) loadEmbIframe();
}

// Lazy-load any iframe whose content is stashed in data-srcdoc — the same
// opacity:0 ≠ WebGL problem that affects the Projector also affects
// 3d-force-graph.  Promote data-srcdoc → srcdoc on first tab activation,
// after which the iframe stays loaded for the rest of the session.
function promoteDataSrcdoc(paneEl) {
  const iframe = paneEl.querySelector("iframe[data-srcdoc]");
  if (!iframe) return;
  iframe.srcdoc = iframe.dataset.srcdoc;
  iframe.removeAttribute("data-srcdoc");
}

tabs.forEach(tab => {
  tab.addEventListener("click", () => {
    tabs.forEach(t => t.classList.remove("active"));
    panes.forEach(p => p.classList.remove("active"));
    tab.classList.add("active");
    const paneEl = document.getElementById(tab.dataset.pane);
    paneEl.classList.add("active");

    // Lazy content loads on first activation.
    promoteDataSrcdoc(paneEl);
    if (tab.dataset.pane === "pane-embeddings" && !embLoaded) {
      loadEmbIframe();
    }
  });
});

simpleBtn.addEventListener("click", () => setEmbMode(false));
advancedBtn.addEventListener("click", () => setEmbMode(true));
</script>
</body>
</html>"""


def _dashboard_html(
    graph: dict[str, Any],
    emb_count: int,
    *,
    layout: str,
) -> str:
    """Build the dashboard chrome.  The Embeddings tab iframes to `projector/`,
    which is served as a sibling directory by the HTTP server."""

    inner_2d = _generate_graph_html(graph, layout=layout, three_d=False)
    inner_3d = _generate_graph_html(graph, layout=layout, three_d=True)

    iframe_2d = _html.escape(inner_2d, quote=True)
    iframe_3d = _html.escape(inner_3d, quote=True)

    out = _DASHBOARD_TEMPLATE
    out = out.replace("__NODE_COUNT__", str(len(graph["nodes"])))
    out = out.replace("__EDGE_COUNT__", str(len(graph["edges"])))
    out = out.replace("__EMB_COUNT__", str(emb_count))
    out = out.replace("__IFRAME_2D__", iframe_2d)
    out = out.replace("__IFRAME_3D__", iframe_3d)
    return out


def _prepare_dashboard_serve_dir(
    graph: dict[str, Any],
    emb_nodes: list[dict[str, Any]],
    *,
    layout: str,
) -> Path:
    """Create a tempdir with dashboard index.html + projector/ subdir.

    Extracted from the command body so tests can verify the layout without
    starting a server.
    """
    serve_dir = Path(tempfile.mkdtemp(prefix="cgraph-dashboard-"))

    html = _dashboard_html(graph, len(emb_nodes), layout=layout)
    (serve_dir / "index.html").write_text(html, encoding="utf-8")

    projector_dir = serve_dir / "projector"
    copy_vendored_projector(projector_dir)
    from ..viz_server import DATA_SUBDIR as _DATA_SUBDIR
    write_projector_data(projector_dir / _DATA_SUBDIR, emb_nodes)

    return serve_dir


def viz_dashboard_command(
    port: int = typer.Option(
        0,
        "--port", "-p",
        help="Port to bind (0 = let the kernel pick a free one).",
    ),
    limit: int = typer.Option(
        500,
        "--limit",
        help="Max nodes per table to fetch for the graph panes.",
    ),
    layout: str = typer.Option(
        "cose",
        "--layout",
        help=f"Initial 2D layout ({', '.join(_LAYOUTS)}). In-browser switcher is still available.",
    ),
    no_open: bool = typer.Option(
        False,
        "--no-open",
        help="Start the server but don't open the browser.",
    ),
) -> None:
    """Unified dashboard: 2D graph, 3D graph, and TF Embedding Projector.

    Starts a local HTTP server; blocks until Ctrl-C.  Needed because the
    Embeddings tab fetches its config via real HTTP (can't be srcdoc-inlined).
    """

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

    print("Fetching embeddings...", file=sys.stderr)
    emb_nodes = fetch_embedded_nodes(conn)

    serve_dir = _prepare_dashboard_serve_dir(graph, emb_nodes, layout=layout)
    bound_port = find_free_port(port or None)
    server = build_server(serve_dir, bound_port)
    url = f"http://127.0.0.1:{bound_port}/"

    typer.echo(emit_json({
        "ok": True,
        "kind": "viz_dashboard_serving",
        "nodes": len(graph["nodes"]),
        "edges": len(graph["edges"]),
        "embeddings": len(emb_nodes),
        "layout": layout,
        "serve_dir": str(serve_dir),
        "url": url,
    }))
    print(
        f"\ncgraph dashboard: serving at {url}\n"
        f"(Ctrl-C to stop)",
        file=sys.stderr,
    )

    serve_until_interrupted(server, url, no_open=no_open, cleanup_dir=serve_dir)
    raise typer.Exit(code=0)
