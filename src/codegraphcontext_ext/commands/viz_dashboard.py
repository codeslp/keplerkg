"""kkg viz-dashboard: server-backed 3-tab viz dashboard.

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
<title>KeplerKG — Dashboard</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='4' fill='%230d1117'/%3E%3Ccircle cx='10' cy='11' r='2.5' fill='%237ee787'/%3E%3Ccircle cx='22' cy='8' r='2' fill='%23f778ba'/%3E%3Ccircle cx='16' cy='20' r='3' fill='%2358a6ff'/%3E%3Ccircle cx='25' cy='22' r='2' fill='%23d2a8ff'/%3E%3Ccircle cx='7' cy='24' r='1.8' fill='%238b949e'/%3E%3Cline x1='10' y1='11' x2='16' y2='20' stroke='%232ea043' stroke-width='0.8' opacity='0.7'/%3E%3Cline x1='22' y1='8' x2='16' y2='20' stroke='%2358a6ff' stroke-width='0.8' opacity='0.7'/%3E%3Cline x1='16' y1='20' x2='25' y2='22' stroke='%23f0883e' stroke-width='0.8' opacity='0.7'/%3E%3Cline x1='16' y1='20' x2='7' y2='24' stroke='%23d2a8ff' stroke-width='0.8' opacity='0.7'/%3E%3Cline x1='10' y1='11' x2='22' y2='8' stroke='%2358a6ff' stroke-width='0.6' opacity='0.4'/%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Antic&family=Antic+Didone&family=Antic+Slab&display=swap" rel="stylesheet">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  /* Antic type system:
     - Antic Didone  → display headings, banner, section titles (high contrast serif)
     - Antic Slab    → body copy, buttons, labels (readable slab workhorse — default)
     - Antic         → chrome/numerics/stats/kbd/placeholders (clean humanist sans) */
  body { font-family: "Antic Slab", Georgia, "Times New Roman", serif;
         background: #0d1117; color: #c9d1d9; overflow: hidden; height: 100vh; display: flex; flex-direction: column; }
  button, input, select, textarea { font-family: inherit; }
  #nav h1 { font-family: "Antic Didone", "Antic Slab", Georgia, serif; }
  #nav .stats { font-family: "Antic", "Antic Slab", Georgia, sans-serif; letter-spacing: 0.02em; }
  .emb-explainer__body h3 { font-family: "Antic Didone", "Antic Slab", Georgia, serif;
                            letter-spacing: 0.12em; }
  .emb-explainer__body kbd { font-family: "Antic", "Antic Slab", Georgia, sans-serif; }
  /* Square every control: no rounded corners anywhere in the dashboard chrome. */
  .tab, .emb-explainer__controls button, .emb-explainer__chevron,
  .emb-explainer__body kbd { border-radius: 0 !important; }
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

  /* Embeddings pane: cgraph-styled explainer panel above the Projector iframe.
     Expanded by default; "Hide tips" collapses the body to just the lede. */
  .embeddings-wrap { display: flex; flex-direction: column; width: 100%; height: 100%; }
  /* Explainer ribbon — compact by default so it takes as little vertical
     space as possible while still carrying a lede + the 3-col tip grid.  The
     ribbon auto-shrinks when any column is empty (grid auto-rows). */
  .emb-explainer { flex-shrink: 0; background: linear-gradient(180deg, #161b22 0%, #1c2128 100%);
                   border-bottom: 1px solid #30363d; transition: padding 0.18s ease-out; }
  .emb-explainer__bar { display: flex; align-items: center; gap: 12px; padding: 8px 16px;
                        transition: padding 0.18s ease-out; }
  .emb-explainer__lede { flex: 1; font-size: 12px; color: #e6edf3; line-height: 1.45; max-width: 80ch; }
  .emb-explainer__lede strong { color: #58a6ff; font-weight: 600; }
  .emb-explainer__lede em { color: #f0883e; font-style: normal; }
  .emb-explainer__controls { display: flex; gap: 6px; flex-shrink: 0; align-items: center; }
  .emb-explainer__controls button {
    padding: 4px 10px; font-size: 11px; color: #9ba6b3; background: transparent;
    border: 1px solid #30363d; cursor: pointer; font-family: inherit;
    transition: color 0.12s, border-color 0.12s, background 0.12s;
  }
  .emb-explainer__controls button:hover { color: #e6edf3; border-color: #58a6ff; background: #1f242d; }
  .emb-explainer__controls button.active { color: #58a6ff; border-color: #58a6ff; background: #1f242d; }
  .emb-explainer__chevron {
    width: 22px; height: 22px; padding: 0 !important;
    display: inline-flex; align-items: center; justify-content: center;
    line-height: 1;
  }
  .emb-explainer__chevron .chev { display: inline-block; font-size: 10px; transition: transform 0.18s ease-out; }
  .emb-explainer.collapsed .emb-explainer__chevron .chev { transform: rotate(-90deg); }
  .emb-explainer__body { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px;
                          padding: 2px 16px 10px 16px; border-top: 1px solid #21262d;
                          font-size: 11px; line-height: 1.45; color: #9ba6b3; }
  .emb-explainer__body h3 { color: #f0883e; font-size: 9px; font-weight: 700;
                             letter-spacing: 0.1em; text-transform: uppercase;
                             margin: 8px 0 3px 0; }
  .emb-explainer__body p { margin: 0 0 3px 0; }
  .emb-explainer__body strong { color: #e6edf3; font-weight: 600; }
  .emb-explainer__body kbd { display: inline-block; padding: 0 5px; font-size: 10px;
                              background: #0d1117; border: 1px solid #30363d;
                              color: #e6edf3; font-family: ui-monospace, SFMono-Regular, monospace; }
  /* Collapsed ribbon: hide tips + lede, tighten the bar to just the button row. */
  .emb-explainer.collapsed .emb-explainer__body { display: none; }
  .emb-explainer.collapsed .emb-explainer__lede { display: none; }
  .emb-explainer.collapsed .emb-explainer__bar { padding: 3px 10px; justify-content: flex-end; }
  .embeddings-wrap iframe { flex: 1; min-height: 0; }

  /* About button — visible, not muted, right side of nav */
  #about-btn { color: #c9d1d9; background: transparent; border: none; cursor: pointer;
               font-family: inherit; font-size: 13px; padding: 8px 16px;
               transition: color 0.12s; }
  #about-btn:hover { color: #58a6ff; }
  /* Modal overlay + card */
  .modal-overlay { position: fixed; inset: 0; z-index: 100; background: rgba(0,0,0,0.6);
                   display: flex; align-items: center; justify-content: center; }
  .modal { background: #161b22; border: 1px solid #30363d; width: 640px; max-height: 80vh;
           overflow-y: auto; padding: 32px; color: #c9d1d9; position: relative;
           display: flex; flex-direction: column; }
  .modal h2 { font-family: "Antic Didone", "Antic Slab", Georgia, serif;
              font-size: 22px; margin-bottom: 16px; color: #e6edf3; }
  .modal h3 { font-family: "Antic Didone", "Antic Slab", Georgia, serif;
              font-size: 11px; color: #f0883e; text-transform: uppercase;
              letter-spacing: 0.12em; margin: 16px 0 6px 0; }
  .modal p { font-size: 13px; line-height: 1.6; margin: 0 0 8px 0; }
  .modal ul { font-size: 13px; line-height: 1.6; margin: 0 0 8px 16px; }
  .modal-close { position: absolute; top: 12px; right: 12px; background: transparent;
                 border: 1px solid #30363d; color: #8b949e; width: 28px; height: 28px;
                 cursor: pointer; font-size: 16px; display: flex; align-items: center;
                 justify-content: center; border-radius: 0 !important; }
  .modal-close:hover { color: #e6edf3; border-color: #58a6ff; }
  .modal .credits { margin-top: auto; padding-top: 16px; border-top: 1px solid #21262d; }
  .modal table { font-size: 12px; width: 100%; border-collapse: collapse; }
  .modal th { text-align: left; color: #8b949e; font-size: 10px; text-transform: uppercase;
              letter-spacing: 0.1em; padding: 4px 8px; border-bottom: 1px solid #30363d; }
  .modal td { padding: 4px 8px; border-bottom: 1px solid #21262d; }
  .modal a { color: #58a6ff; text-decoration: none; }
  .modal a:hover { text-decoration: underline; }
</style>
</head>
<body>
<div id="nav">
  <h1>KeplerKG</h1>
  <div class="stats">__NODE_COUNT__ nodes &middot; __EDGE_COUNT__ edges &middot; __EMB_COUNT__ embeddings</div>
  <div class="tab-bar" id="tab-bar">
    <button class="tab active" data-pane="pane-2d">2D Graph</button>
    <button class="tab" data-pane="pane-3d">3D Graph</button>
    <button class="tab" data-pane="pane-embeddings">Embeddings</button>
  </div>
  <button type="button" id="about-btn">About</button>
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
      <section class="emb-explainer" id="emb-explainer" aria-label="Embedding projector guide">
        <div class="emb-explainer__bar">
          <div class="emb-explainer__lede">
            <strong>Each dot is a function.</strong> Points are placed by what a routine <em>does</em>, not by what it&rsquo;s called &mdash; so two functions that solve the same problem cluster together even when their names disagree.
          </div>
          <div class="emb-explainer__controls">
            <button type="button" id="emb-simple-btn" class="active">Clean</button>
            <button type="button" id="emb-advanced-btn">Advanced</button>
            <button type="button" id="emb-explainer-toggle" class="emb-explainer__chevron" aria-expanded="true" aria-controls="emb-explainer-body" title="Hide tips"><span class="chev">&#9662;</span></button>
          </div>
        </div>
        <div class="emb-explainer__body" id="emb-explainer-body">
          <div>
            <h3>What you&rsquo;re looking at</h3>
            <p>Every dot is one function, method, or class pulled from your repo&rsquo;s graph.</p>
            <p><strong>Nearby dots</strong> do semantically similar work &mdash; similar inputs, similar shape, similar intent.</p>
            <p><strong>Distance matters; the axes don&rsquo;t.</strong> Projections rotate the cloud freely, so &ldquo;up&rdquo; and &ldquo;right&rdquo; carry no meaning on their own.</p>
          </div>
          <div>
            <h3>How to interact</h3>
            <p><strong>Click a dot</strong> &rarr; its nearest semantic neighbours appear in the right rail.</p>
            <p><strong>Drag / scroll</strong> to orbit and zoom in 3D.</p>
            <p><strong>Search</strong> (top-right) jumps to a named function &mdash; try a substring like <kbd>validate</kbd>.</p>
            <p><strong>Isolate selection</strong> (inspector panel) zooms the view to just the points you&rsquo;ve picked.</p>
          </div>
          <div>
            <h3>Projection modes <span style="color:#9ba6b3;font-weight:400;text-transform:none;letter-spacing:0">(tabs at bottom-left)</span></h3>
            <p><strong>UMAP</strong> &mdash; the clearest clusters. Start here when hunting duplicates or refactor candidates.</p>
            <p><strong>t-SNE</strong> &mdash; sharp local groups, but the <em>distance between</em> clusters is not meaningful.</p>
            <p><strong>PCA</strong> &mdash; linear; useful to see overall spread and which directions carry the most variance.</p>
          </div>
        </div>
      </section>
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
<div class="modal-overlay" id="about-overlay" style="display:none">
  <div class="modal">
    <button type="button" class="modal-close" id="about-close">&times;</button>
    <h2>KeplerKG</h2>
    <h3>Purpose</h3>
    <p>KeplerKG exists to make the creation of knowledge graphs and embeddings for institutional knowledge of all kinds &mdash; code is the pilot domain, not the ceiling. The code-graph work is a beachhead; the generalised goal is turning any corpus (documentation, meeting transcripts, ticket histories, process wikis) into a navigable graph and embedding space that surfaces structure, similarity, and drift automatically.</p>
    <h3>Future Plans</h3>
    <ul>
      <li>Generalise beyond source code to institutional corpora (docs, meeting notes, ticket histories, process wikis)</li>
      <li>MCP server mode for agentic retrieval against a KeplerKG graph</li>
      <li>Drift detection + advisories on stale or contradicted knowledge</li>
      <li>Scale to larger repos / multi-corpus federation</li>
    </ul>
    <div class="credits">
      <h3>Credits</h3>
      <table>
        <tr><th>Tool</th><th>License</th></tr>
        <tr><td>KeplerKG</td><td>MIT</td></tr>
        <tr><td><a href="https://github.com/Vi-Sri/CodeGraphContext">codegraphcontext</a></td><td>Apache 2.0</td></tr>
        <tr><td><a href="https://github.com/tensorflow/embedding-projector-standalone">TensorFlow Embedding Projector</a></td><td>Apache 2.0 &copy; Google</td></tr>
        <tr><td><a href="https://js.cytoscape.org/">Cytoscape.js</a></td><td>MIT</td></tr>
        <tr><td><a href="https://github.com/vasturiano/3d-force-graph">3d-force-graph</a></td><td>MIT</td></tr>
        <tr><td><a href="https://kuzudb.com/">K&ugrave;zuDB</a></td><td>MIT</td></tr>
        <tr><td><a href="https://sbert.net/">sentence-transformers</a></td><td>Apache 2.0</td></tr>
      </table>
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
  if (embLoaded) loadEmbIframe();
}

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
    promoteDataSrcdoc(paneEl);
    if (tab.dataset.pane === "pane-embeddings" && !embLoaded) {
      loadEmbIframe();
    }
  });
});

simpleBtn.addEventListener("click", () => setEmbMode(false));
advancedBtn.addEventListener("click", () => setEmbMode(true));

// Collapse / expand the explainer body.
const explainer = document.getElementById("emb-explainer");
const explainerToggle = document.getElementById("emb-explainer-toggle");
explainerToggle.addEventListener("click", () => {
  const collapsed = explainer.classList.toggle("collapsed");
  explainerToggle.setAttribute("aria-expanded", String(!collapsed));
  explainerToggle.setAttribute("title", collapsed ? "Show tips" : "Hide tips");
});

// About modal — DOM is above this script so elements are guaranteed non-null.
const aboutBtn = document.getElementById("about-btn");
const aboutOverlay = document.getElementById("about-overlay");
const aboutClose = document.getElementById("about-close");
aboutBtn.addEventListener("click", () => { aboutOverlay.style.display = "flex"; });
aboutClose.addEventListener("click", () => { aboutOverlay.style.display = "none"; });
aboutOverlay.addEventListener("click", (e) => {
  if (e.target === aboutOverlay) aboutOverlay.style.display = "none";
});
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
            "detail": "No nodes found. Run `kkg index` first.",
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
