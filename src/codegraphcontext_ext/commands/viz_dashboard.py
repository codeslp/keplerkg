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
import os
import re
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Optional

import typer

from ..embeddings.fetch import fetch_embedded_nodes
from ..embeddings.runtime import probe_backend_support
from ..io.json_stdout import emit_json
from ..io.kuzu import get_kuzu_connection
from ..project import PROJECT_OPTION_HELP, activate_project
from ..viz_server import (
    DATA_SUBDIR,
    VENDOR_FILES,
    build_server,
    copy_vendored_projector,
    find_free_port,
    serve_until_interrupted,
    write_projector_data,
)
from ..loading_animation import LOADING_CSS, LOADING_JS, loader_html
from .viz_graph import (
    _LAYOUTS,
    _fetch_graph,
    _generate_html as _generate_graph_html,
)

COMMAND_NAME = "viz-dashboard"
SCHEMA_FILE = "context.json"
SUMMARY = "Unified dashboard: 2D graph, 3D graph, embeddings scatter, and TF Projector as tabs."

_CATEGORY_PRINCIPLES: dict[str, str] = {
    "clarity": "Public interfaces should explain intent and expectations clearly.",
    "compliance": "Security- and trust-sensitive flows should be controlled, auditable, and safe by default.",
    "complexity": "Keep units small and focused enough that one reader can understand and test them end to end.",
    "coupling": "Keep dependencies directional and module boundaries explicit so change stays local.",
    "dead_code": "Keep the public surface area intentional, discoverable, and actually used.",
    "inheritance": "Prefer shallow hierarchies and explicit composition over deep inheritance.",
    "naming": "Names should match behavior and reinforce a consistent vocabulary across the codebase.",
}

_RULE_PRINCIPLES: dict[str, str] = {
    "admin_action_no_audit_trail": "Privileged mutations should leave an audit trail so changes remain attributable.",
    "auth_bypass": "Authorization belongs on the path to sensitive data or state changes, not as an optional convention.",
    "circular_imports": "Modules should depend in one direction so they stay independently understandable and testable.",
    "class_too_large": "A class should stay cohesive enough to have one clear reason to change.",
    "cross_file_private_access": "Respect module boundaries and keep private implementation details private.",
    "deep_inheritance": "Prefer composition and shallow hierarchies over deep inheritance chains.",
    "error_handler_leaks_internals": "Failures should be observable without exposing internal implementation details.",
    "excessive_fan_out": "Keep control flow narrow enough that one function remains easy to reason about.",
    "function_cyclomatic_complexity": "Keep branching under control so behavior stays understandable and testable.",
    "function_too_long": "Keep routines short enough that the whole flow fits in one reader's head.",
    "hardcoded_secret_in_graph": "Secrets must be managed outside source code and outside the graph built from it.",
    "inconsistent_naming": "Semantically similar behavior should use consistent names so the codebase forms a stable vocabulary.",
    "misleading_name": "A symbol's name should describe what the code actually does, not what it once did or hoped to do.",
    "missing_docstring_public": "Public interfaces should document intent, constraints, and expected use.",
    "module_content_mismatch": "A module name should reflect the work its contents actually perform.",
    "parameter_count": "Function interfaces should stay small enough that the call contract remains cohesive.",
    "rate_limit_missing": "External-facing operations should enforce backpressure so one caller cannot overwhelm the system.",
    "sensitive_data_unprotected": "Sensitive data should be masked, minimized, or encrypted before it leaves trusted boundaries.",
    "separation_of_duties_violation": "Separate business logic from infrastructure concerns so each layer can evolve safely.",
    "suggest_better_name": "Prefer names that align with the established vocabulary of similar code.",
    "test_import_in_prod": "Production code should not depend on test-only helpers, fixtures, or scaffolding.",
    "unlogged_endpoint": "Request handling should leave an operational trail for debugging, forensics, and accountability.",
    "unreferenced_public_class": "Unused public APIs create maintenance burden and should not survive by accident.",
    "unreferenced_public_function": "Unused public APIs create maintenance burden and should not survive by accident.",
}


def _rule_principle(rule_id: str, category: str) -> str:
    """Return the human-facing quality principle behind a standards rule."""
    return _RULE_PRINCIPLES.get(rule_id) or _CATEGORY_PRINCIPLES.get(
        category,
        "Each rule protects a code-quality boundary so behavior stays understandable, maintainable, and safe to change.",
    )


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
  .emb-explainer__panel { padding: 10px 12px; border: 1px solid #21262d; background: #11161d;
                          transition: border-color 0.12s, background 0.12s, box-shadow 0.12s; }
  .emb-explainer__panel.active { border-color: #58a6ff; background: #161b22;
                                 box-shadow: inset 0 0 0 1px rgba(88,166,255,0.22); }
  .emb-explainer__panel p:last-child { margin-bottom: 0; }
  .emb-explainer__eyebrow { display: inline-block; margin-bottom: 6px; font-size: 10px;
                            letter-spacing: 0.08em; text-transform: uppercase; color: #58a6ff; }
  .emb-explainer__badge { display: inline-flex; align-items: center; padding: 4px 10px;
                          font-size: 11px; color: #58a6ff; background: #0d1117;
                          border: 1px solid #30363d; }
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
  .std-offender-table-wrap {
    width: 100%;
    max-width: 100%;
    overflow: hidden;
    border: 1px solid #21262d;
    background: #0d1117;
  }
  .std-offender-table {
    width: 100%;
    max-width: 100%;
    border-collapse: collapse;
    table-layout: fixed;
  }
  .std-offender-table thead th {
    position: relative;
    padding: 4px 8px;
    border-bottom: 1px solid #21262d;
    color: #8b949e;
    font-size: 11px;
    font-weight: 500;
    text-align: left;
    background: #0d1117;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .std-offender-table td {
    padding: 4px 8px;
    border-bottom: 1px solid #161b22;
    color: #8b949e;
    font-size: 11px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    vertical-align: middle;
  }
  .std-offender-table td.std-cell-name {
    color: #e6edf3;
    font-family: ui-monospace, SFMono-Regular, monospace;
  }
  .std-offender-table td.std-cell-path { font-size: 10px; }
  .std-offender-table td.std-cell-line,
  .std-offender-table td.std-cell-metric,
  .std-offender-table td.std-cell-viz,
  .std-offender-table th.std-head-line,
  .std-offender-table th.std-head-metric,
  .std-offender-table th.std-head-viz {
    text-align: center;
  }
  .std-viz-btn {
    padding: 2px 6px;
    font-size: 10px;
    background: #161b22;
    color: #58a6ff;
    border: 1px solid #30363d;
    cursor: pointer;
  }
  .std-viz-btn:hover { border-color: #58a6ff; color: #e6edf3; }
  .std-col-resizer {
    position: absolute;
    top: 0;
    right: -5px;
    width: 10px;
    height: 100%;
    cursor: col-resize;
    user-select: none;
    touch-action: none;
  }
  .std-col-resizer::after {
    content: "";
    position: absolute;
    top: 20%;
    bottom: 20%;
    left: 4px;
    width: 1px;
    background: #30363d;
  }
__LOADING_CSS__
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
    <button class="tab" data-pane="pane-standards">Standards</button>
    <button class="tab" data-pane="pane-taxonomy">Taxonomy</button>
  </div>
  <button type="button" id="about-btn">About</button>
</div>
<div id="panes">
  <!-- 2D pane is visible on load; srcdoc set immediately.  3D pane is
       hidden on load and Chrome refuses WebGL for invisible iframes — so
       its srcdoc is stashed as data-srcdoc and promoted on first tab
       click by the JS below, where the iframe is actually visible. -->
  <div class="pane active" id="pane-2d">
    __LOADER_2D__
    <iframe id="iframe-2d" srcdoc="__IFRAME_2D__"></iframe>
  </div>
  <div class="pane" id="pane-3d">
    __LOADER_3D__
    <iframe id="iframe-3d" data-srcdoc="__IFRAME_3D__"></iframe>
  </div>
  <div class="pane" id="pane-embeddings">
    __LOADER_EMB__
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
  <div class="pane" id="pane-standards">
    __LOADER_STD__
    <div style="display:flex;flex-direction:column;width:100%;height:100%;position:absolute;inset:0;font-family:'Antic',sans-serif;color:#c9d1d9">
      <!-- Sub-tab bar -->
      <div style="display:flex;align-items:center;gap:0;background:#0d1117;border-bottom:2px solid #30363d;flex-shrink:0;padding:0 12px">
        <button class="tab active" data-std-pane="std-violations" style="padding:8px 16px;font-size:13px;color:#58a6ff;background:transparent;border:none;border-bottom:2px solid #58a6ff;cursor:pointer;margin-bottom:-2px">Violations</button>
        <button class="tab" data-std-pane="std-config" style="padding:8px 16px;font-size:13px;color:#8b949e;background:transparent;border:none;border-bottom:2px solid transparent;cursor:pointer;margin-bottom:-2px">Configuration</button>
        <span style="flex:1"></span>
        <span style="font-size:11px;color:#8b949e" id="std-viol-count"></span>
      </div>

      <!-- VIOLATIONS SUB-TAB -->
      <div id="std-violations" style="flex:1;overflow-y:auto;padding:12px 16px">
        <section class="emb-explainer" id="std-viol-explainer" aria-label="Standards violations guide" style="margin-bottom:12px">
          <div class="emb-explainer__bar">
            <div class="emb-explainer__lede">
              <strong>Each card is one rule that currently fires.</strong> Open a card to inspect the offending symbols, then jump into the 2D graph or the configuration view to understand and tune what you&rsquo;re seeing.
            </div>
            <div class="emb-explainer__controls">
              <button type="button" id="std-viol-explainer-toggle" class="emb-explainer__chevron" aria-expanded="true" aria-controls="std-viol-explainer-body" title="Hide tips"><span class="chev">&#9662;</span></button>
            </div>
          </div>
          <div class="emb-explainer__body" id="std-viol-explainer-body">
            <div>
              <h3>What this tab shows</h3>
              <p><strong>Each card</strong> is one standard that currently fires against the live graph.</p>
              <p><strong>Severity dot</strong> marks whether the hit is a <kbd>warn</kbd> or <kbd>hard</kbd> finding.</p>
              <p><strong>Offender count</strong> on the right tells you how many symbols matched that rule.</p>
            </div>
            <div>
              <h3>What the table means</h3>
              <p><strong>Name / File / Line</strong> identify the offending symbol.</p>
              <p><strong>Metric</strong> shows the rule-specific value or matched target that caused the hit.</p>
              <p><strong>2D button</strong> jumps straight to that offender inside the graph view.</p>
            </div>
            <div>
              <h3>What to do next</h3>
              <p><strong>Open a card</strong> to inspect evidence, suggestion text, and all matched offenders.</p>
              <p><strong>Configure this rule</strong> jumps to the configuration sub-tab for severity or enablement changes.</p>
              <p><strong>Header count</strong> summarizes total fired rows across the whole tab.</p>
            </div>
          </div>
        </section>
        <div id="std-viol-body"></div>
      </div>

      <!-- CONFIGURATION SUB-TAB (hidden initially) -->
      <div id="std-config" style="flex:1;overflow-y:auto;display:none;flex-direction:column">
        <div style="padding:8px 16px;display:flex;align-items:center;gap:12px;background:#0d1117;border-bottom:1px solid #21262d;flex-shrink:0">
          <label style="font-size:11px;color:#8b949e">Profile:</label>
          <select id="std-profile" style="padding:4px 8px;background:#161b22;color:#c9d1d9;border:1px solid #30363d;border-radius:4px;font-size:12px">
            <option value="default">Default</option><option value="strict">Strict</option>
            <option value="soc2">SOC 2</option><option value="minimal">Minimal</option>
          </select>
          <span style="flex:1"></span>
          <span style="font-size:11px;color:#8b949e" id="std-summary"></span>
          <button id="std-export" style="padding:5px 12px;background:#238636;color:#fff;border:none;border-radius:4px;font-size:11px;cursor:pointer">Export TOML</button>
        </div>
        <div style="flex:1;overflow-y:auto;padding:0">
          <table style="width:100%;border-collapse:collapse;font-size:12px" id="std-table">
            <thead style="position:sticky;top:0;background:#0d1117;z-index:1">
              <tr style="border-bottom:2px solid #30363d;text-align:left">
                <th style="padding:8px 12px;width:40px">On</th>
                <th style="padding:8px 8px">Rule</th>
                <th style="padding:8px 8px">Category</th>
                <th style="padding:8px 8px;width:100px">Severity</th>
                <th style="padding:8px 8px;width:24px"></th>
                <th style="padding:8px 12px">Summary</th>
              </tr>
            </thead>
            <tbody id="std-tbody"></tbody>
          </table>
        </div>
      </div>
    </div>
    <script>
    (function() {
      const rulesData = __STANDARDS_JSON__;
      const violationsData = __VIOLATIONS_JSON__;
      const sevColors = {hard:'#f85149',warn:'#d29922'};
      const catColors = {coupling:'#f0883e',complexity:'#d29922',dead_code:'#484f58',clarity:'#58a6ff',inheritance:'#d2a8ff',compliance:'#f85149',naming:'#a5d6ff'};

      function sevPill(sev) {
        const bg = sevColors[sev] || '#8b949e';
        return '<span style="display:inline-block;padding:1px 7px;border-radius:4px;font-size:10px;font-weight:600;letter-spacing:0.03em;color:#fff;background:'+bg+'">'+((sev||'').toUpperCase())+'</span>';
      }
      function catPill(cat, count) {
        const c = catColors[cat] || '#8b949e';
        const label = (cat||'').replace(/_/g,' ');
        return '<span style="display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:4px;font-size:11px;color:'+c+';border:1px solid '+c+'33;background:'+c+'11">'
          + label + (count != null ? ' <strong>'+count+'</strong>' : '') + '</span>';
      }
      let overrides = {};
      let disabled = new Set();
      let collapsedCats = new Set();
      const offenderColumns = [
        { key: 'name', label: 'Name', width: '18%', min: 120 },
        { key: 'file', label: 'File', width: '44%', min: 240 },
        { key: 'line', label: 'Line', width: '10%', min: 72 },
        { key: 'metric', label: 'Metric', width: '16%', min: 96 },
        { key: 'viz', label: 'Viz', width: '12%', min: 72 },
      ];

      function escapeHtml(value) {
        return String(value ?? '').replace(/[&<>"']/g, ch => ({
          '&': '&amp;',
          '<': '&lt;',
          '>': '&gt;',
          '"': '&quot;',
          "'": '&#39;',
        }[ch]));
      }

      function renderOffenderTable(offenders) {
        const cols = offenderColumns.map(col =>
          '<col data-col-key="' + col.key + '" data-min-width="' + col.min + '" style="width:' + col.width + '">'
        ).join('');
        const heads = offenderColumns.map((col, index) =>
          '<th class="std-head-' + col.key + '" data-col-key="' + col.key + '">' +
            escapeHtml(col.label) +
            (index < offenderColumns.length - 1
              ? '<span class="std-col-resizer" data-col-resizer="' + col.key + '"></span>'
              : '') +
          '</th>'
        ).join('');
        const rows = (offenders || []).map(o => {
          const uid = escapeHtml(o.uid || '');
          const name = escapeHtml(o.name || '—');
          const path = escapeHtml(o.path || '—');
          const line = escapeHtml(o.line_number != null ? o.line_number : '—');
          const metric = escapeHtml(o.metric_value != null ? o.metric_value : '—');
          return '<tr>' +
            '<td class="std-cell-name" title="' + name + '">' + name + '</td>' +
            '<td class="std-cell-path" title="' + path + '">' + path + '</td>' +
            '<td class="std-cell-line" title="' + line + '">' + line + '</td>' +
            '<td class="std-cell-metric" title="' + metric + '">' + metric + '</td>' +
            '<td class="std-cell-viz">' +
              '<button class="std-viz-btn" data-viz-id="' + uid + '" data-viz-name="' + name + '" title="Show in 2D graph">2D</button>' +
            '</td>' +
          '</tr>';
        }).join('');
        return '<div class="std-offender-table-wrap">' +
          '<table class="std-offender-table" style="table-layout:fixed">' +
            '<colgroup>' + cols + '</colgroup>' +
            '<thead><tr>' + heads + '</tr></thead>' +
            '<tbody>' + rows + '</tbody>' +
          '</table>' +
        '</div>';
      }

      function setupOffenderTableResizers(root) {
        root.querySelectorAll('.std-offender-table').forEach(table => {
          if (table.dataset.resizeReady === '1') return;
          table.dataset.resizeReady = '1';
          const cols = Array.from(table.querySelectorAll('col[data-col-key]'));
          const lookup = Object.fromEntries(cols.map((col, index) => [col.dataset.colKey, index]));
          table.querySelectorAll('[data-col-resizer]').forEach(handle => {
            handle.addEventListener('mousedown', event => {
              event.preventDefault();
              event.stopPropagation();
              const key = handle.dataset.colResizer;
              const index = lookup[key];
              const current = cols[index];
              const next = cols[index + 1];
              if (!current || !next) return;
              const startX = event.clientX;
              const startCurrent = current.getBoundingClientRect().width;
              const startNext = next.getBoundingClientRect().width;
              const minCurrent = Number(current.dataset.minWidth || 72);
              const minNext = Number(next.dataset.minWidth || 72);

              function onMove(moveEvent) {
                const rawDelta = moveEvent.clientX - startX;
                const minDelta = minCurrent - startCurrent;
                const maxDelta = startNext - minNext;
                const delta = Math.max(minDelta, Math.min(rawDelta, maxDelta));
                current.style.width = (startCurrent + delta) + 'px';
                next.style.width = (startNext - delta) + 'px';
              }

              function onUp() {
                window.removeEventListener('mousemove', onMove);
                window.removeEventListener('mouseup', onUp);
              }

              window.addEventListener('mousemove', onMove);
              window.addEventListener('mouseup', onUp);
            });
          });
        });
      }

      // ---- Sub-tab switching ----
      document.querySelectorAll('[data-std-pane]').forEach(btn => {
        btn.addEventListener('click', () => {
          document.querySelectorAll('[data-std-pane]').forEach(b => {
            b.style.color='#8b949e'; b.style.borderBottomColor='transparent'; b.classList.remove('active');
          });
          btn.style.color='#58a6ff'; btn.style.borderBottomColor='#58a6ff'; btn.classList.add('active');
          document.getElementById('std-violations').style.display = btn.dataset.stdPane==='std-violations'?'block':'none';
          document.getElementById('std-config').style.display = btn.dataset.stdPane==='std-config'?'flex':'none';
        });
      });

      // ---- VIOLATIONS TAB ----
      function renderViolations() {
        const body = document.getElementById('std-viol-body');
        if (!body) return;
        body.innerHTML = '';
        const countEl = document.getElementById('std-viol-count');

        if (!violationsData.length) {
          body.innerHTML = '<div style="padding:40px;text-align:center;color:#7ee787"><h3 style="margin-bottom:8px">No violations found</h3><p style="color:#8b949e">All standards pass against the current graph. Run <code>kkg audit --format summary</code> for CLI output.</p></div>';
          if (countEl) countEl.textContent = '0 violations';
          return;
        }

        let totalOffenders = 0;
        const catCounts = {};
        violationsData.forEach(v => {
          totalOffenders += (v.offenders||[]).length;
          const rule = rulesData.find(r => r.id === v.standard_id) || {};
          const cat = rule.category || 'other';
          catCounts[cat] = (catCounts[cat]||0) + (v.offenders||[]).length;
        });
        if (countEl) countEl.textContent = totalOffenders + ' violation' + (totalOffenders!==1?'s':'') + ' across ' + violationsData.length + ' rule' + (violationsData.length!==1?'s':'');

        // Category summary bar
        const catBar = document.createElement('div');
        catBar.style.cssText = 'display:flex;flex-wrap:wrap;gap:6px;margin-bottom:14px;padding:10px 12px;background:#161b22;border:1px solid #21262d;border-radius:6px';
        Object.keys(catCounts).sort().forEach(cat => {
          catBar.innerHTML += catPill(cat, catCounts[cat]);
        });
        body.appendChild(catBar);

        violationsData.forEach(v => {
          const rule = rulesData.find(r => r.id === v.standard_id) || {};
          const section = document.createElement('div');
          section.style.cssText = 'margin-bottom:16px;border:1px solid #21262d;border-radius:6px;overflow:hidden';

          // Header bar
          const header = document.createElement('div');
          header.style.cssText = 'display:flex;align-items:center;padding:10px 14px;background:#161b22;cursor:pointer;gap:10px';
          const arrow = document.createElement('span');
          arrow.textContent = '\u25B6'; arrow.style.cssText = 'font-size:10px;color:#484f58;transition:transform 0.15s';
          header.innerHTML = sevPill(v.severity) + '<strong style="color:#e6edf3;font-size:12px">' + v.standard_id + '</strong>' +
            catPill(rule.category) +
            '<span style="flex:1"></span>' +
            '<span style="font-size:11px;color:#f0883e">' + (v.offenders||[]).length + ' offender' + ((v.offenders||[]).length!==1?'s':'') + '</span>';
          header.prepend(arrow);

          // Body (collapsed by default)
          const detail = document.createElement('div');
          detail.style.cssText = 'display:none;padding:12px 14px;background:#0d1117;border-top:1px solid #21262d';

          // Description
          let desc = '<p style="margin:0 0 8px;font-size:12px;color:#8b949e">' + escapeHtml(rule.summary||v.kind) + '</p>';
          if (rule.principle) {
            desc += '<p style="margin:0 0 10px;font-size:11px;color:#9ecbff;line-height:1.5"><strong style="color:#58a6ff">Principle:</strong> ' + escapeHtml(rule.principle) + '</p>';
          }
          if (v.suggestion) desc += '<p style="margin:0 0 10px;font-size:11px;color:#7ee787">' + escapeHtml(v.suggestion) + '</p>';
          if (rule.evidence) desc += '<p style="margin:0 0 10px;font-size:11px;color:#6e7681;font-style:italic">Evidence: ' + escapeHtml(rule.evidence) + '</p>';

          // Offender list
          desc += renderOffenderTable(v.offenders || []);

          // Link to config tab
          desc += '<div style="margin-top:10px"><button data-goto-config="'+v.standard_id+'" style="padding:4px 10px;font-size:11px;background:#161b22;color:#58a6ff;border:1px solid #30363d;border-radius:4px;cursor:pointer">Configure this rule &rarr;</button></div>';

          detail.innerHTML = desc;

          header.addEventListener('click', () => {
            const open = detail.style.display !== 'none';
            detail.style.display = open ? 'none' : 'block';
            arrow.style.transform = open ? '' : 'rotate(90deg)';
          });

          section.appendChild(header);
          section.appendChild(detail);
          body.appendChild(section);
        });

        // Wire viz buttons
        setupOffenderTableResizers(body);

        body.querySelectorAll('[data-viz-id]').forEach(btn => {
          btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const id = btn.dataset.vizId;
            const name = btn.dataset.vizName;
            // Switch to 2D tab and highlight
            const tab2d = document.querySelector('[data-pane="pane-2d"]');
            if (tab2d) { tab2d.click(); }
            // Post message to 2D iframe to highlight node
            const iframe2d = document.getElementById('iframe-2d');
            if (iframe2d && iframe2d.contentWindow) {
              setTimeout(() => {
                iframe2d.contentWindow.postMessage({type:'highlight',id:id,name:name}, '*');
              }, 500);
            }
          });
        });

        // Wire config links
        body.querySelectorAll('[data-goto-config]').forEach(btn => {
          btn.addEventListener('click', (e) => {
            e.stopPropagation();
            document.querySelector('[data-std-pane="std-config"]').click();
          });
        });
      }

      // ---- CONFIGURATION TAB ----
      function renderTable() {
        const tbody = document.getElementById('std-tbody');
        if (!tbody) return;
        tbody.innerHTML = '';
        const cats = [...new Set(rulesData.map(r=>r.category))].sort();
        let warnCount=0, hardCount=0, offCount=0;

        cats.forEach(cat => {
          const isCollapsed = collapsedCats.has(cat);
          const catRow = document.createElement('tr');
          catRow.style.cssText='background:#161b22;cursor:pointer';
          const catArrow = isCollapsed ? '\u25B6' : '\u25BC';
          const catRuleCount = rulesData.filter(r=>r.category===cat).length;
          catRow.innerHTML='<td colspan="6" style="padding:8px 12px;border-bottom:1px solid #21262d"><span style="font-size:9px;margin-right:6px;color:#484f58">'+catArrow+'</span>'+catPill(cat, catRuleCount)+'</td>';
          catRow.addEventListener('click', () => {
            if (collapsedCats.has(cat)) collapsedCats.delete(cat); else collapsedCats.add(cat);
            renderTable();
          });
          tbody.appendChild(catRow);

          if (isCollapsed) return;

          rulesData.filter(r=>r.category===cat).forEach(r => {
            const isOff = disabled.has(r.id) || overrides[r.id]==='off';
            const sev = isOff ? 'off' : (overrides[r.id] || r.severity);
            if(sev==='hard') hardCount++; else if(sev==='warn') warnCount++; if(isOff) offCount++;

            const tr = document.createElement('tr');
            tr.style.cssText='border-bottom:1px solid #21262d;'+(isOff?'opacity:0.4':'');

            const evRow = document.createElement('tr');
            evRow.style.cssText='display:none;background:#161b22';
            evRow.innerHTML='<td></td><td colspan="5" style="padding:8px 12px;font-size:11px;line-height:1.6;color:#6e7681">'+
              (r.evidence?'<strong style="color:#58a6ff">Evidence:</strong> '+r.evidence+'<br>':'')+
              (r.suggestion?'<strong style="color:#7ee787">Suggestion:</strong> '+r.suggestion+'<br>':'')+
              (r.thresholds&&Object.keys(r.thresholds).length?'<strong>Thresholds:</strong> '+JSON.stringify(r.thresholds):'')+
              '</td>';

            let isExpanded = false;
            const expandArrow = '<span class="cfg-arrow" style="font-size:9px;color:#484f58;cursor:pointer;display:inline-block;transition:transform 0.15s">\u25B6</span>';

            tr.innerHTML=
              '<td style="padding:6px 12px;text-align:center"><label style="cursor:pointer"><input type="checkbox" data-rule="'+r.id+'" '+(isOff?'':'checked')+' style="accent-color:#238636"></label></td>'+
              '<td style="padding:6px 8px;color:#e6edf3;font-family:monospace;font-size:11px">'+r.id+'</td>'+
              '<td style="padding:6px 8px">'+catPill(r.category)+'</td>'+
              '<td style="padding:6px 8px"><select data-sev="'+r.id+'" style="padding:2px 4px;background:#0d1117;color:'+(sevColors[sev]||'#8b949e')+';border:1px solid #30363d;border-radius:3px;font-size:11px">'+
                '<option value="hard"'+(sev==='hard'?' selected':'')+'>hard</option>'+
                '<option value="warn"'+(sev==='warn'?' selected':'')+'>warn</option>'+
              '</select></td>'+
              '<td style="padding:6px 4px">'+expandArrow+'</td>'+
              '<td style="padding:6px 8px;color:#8b949e">'+r.summary+'</td>';

            tr.addEventListener('click',function(e){
              if(e.target.tagName==='INPUT'||e.target.tagName==='SELECT') return;
              isExpanded = !isExpanded;
              evRow.style.display = isExpanded ? 'table-row' : 'none';
              const a = tr.querySelector('.cfg-arrow');
              if(a) a.style.transform = isExpanded ? 'rotate(90deg)' : '';
            });
            tr.style.cursor='pointer';
            tbody.appendChild(tr);
            tbody.appendChild(evRow);
          });
        });

        const sum = document.getElementById('std-summary');
        if(sum) sum.textContent=hardCount+' hard \u00b7 '+warnCount+' warn \u00b7 '+offCount+' off \u00b7 '+rulesData.length+' total';

        tbody.querySelectorAll('input[data-rule]').forEach(cb=>{
          cb.addEventListener('change',function(){
            if(this.checked) { disabled.delete(this.dataset.rule); delete overrides[this.dataset.rule]; }
            else { disabled.add(this.dataset.rule); overrides[this.dataset.rule]='off'; }
            renderTable();
          });
        });
        tbody.querySelectorAll('select[data-sev]').forEach(sel=>{
          sel.addEventListener('change',function(){
            overrides[this.dataset.sev]=this.value;
            renderTable();
          });
        });
      }

      document.getElementById('std-profile')?.addEventListener('change',function(){
        overrides={};disabled=new Set();
        if(this.value==='minimal') rulesData.forEach(r=>{if(r.category!=='coupling'){disabled.add(r.id);overrides[r.id]='off';}});
        if(this.value==='soc2'){rulesData.forEach(r=>{if(r.category!=='coupling'&&r.category!=='compliance'){disabled.add(r.id);overrides[r.id]='off';}});['auth_bypass','sensitive_data_unprotected','hardcoded_secret_in_graph','admin_action_no_audit_trail'].forEach(id=>overrides[id]='hard');}
        if(this.value==='strict') ['class_too_large','function_cyclomatic_complexity','excessive_fan_out'].forEach(id=>overrides[id]='hard');
        renderTable();
      });

      document.getElementById('std-export')?.addEventListener('click',function(){
        const profile=document.getElementById('std-profile')?.value||'default';
        let lines=['[cgraph.standards]','profile = "'+profile+'"'];
        const activeCats=[...new Set(rulesData.map(r=>r.category))].filter(c=>!rulesData.every(r=>r.category!==c||disabled.has(r.id)));
        lines.push('categories = ['+activeCats.map(c=>'"'+c+'"').join(', ')+']');
        const ovr=Object.entries(overrides).filter(([,v])=>v);
        if(ovr.length){lines.push('','[cgraph.standards.overrides]');ovr.forEach(([k,v])=>lines.push(k+' = "'+v+'"'));}
        const blob=new Blob([lines.join('\\n')],{type:'text/plain'});
        const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='kkg-standards.toml';a.click();
      });

      const stdViolExplainer = document.getElementById('std-viol-explainer');
      const stdViolExplainerToggle = document.getElementById('std-viol-explainer-toggle');
      if (stdViolExplainer && stdViolExplainerToggle) {
        stdViolExplainerToggle.addEventListener('click', () => {
          const collapsed = stdViolExplainer.classList.toggle('collapsed');
          stdViolExplainerToggle.setAttribute('aria-expanded', String(!collapsed));
          stdViolExplainerToggle.setAttribute('title', collapsed ? 'Show tips' : 'Hide tips');
        });
      }

      window._stdInit = function() { renderViolations(); renderTable(); };
    })();
    </script>
  </div>
  <!-- ═══ TAXONOMY TAB ═══ -->
  <div class="pane" id="pane-taxonomy">
    __LOADER_TAX__
    <div style="display:flex;flex-direction:column;width:100%;height:100%;position:absolute;inset:0;font-family:'Antic',sans-serif;color:#c9d1d9">
      <div style="display:flex;align-items:center;gap:0;background:#0d1117;border-bottom:2px solid #30363d;flex-shrink:0;padding:0 12px">
        <button class="tab active" data-tax-pane="tax-structure" style="padding:8px 16px;font-size:13px;color:#58a6ff;background:transparent;border:none;border-bottom:2px solid #58a6ff;cursor:pointer;margin-bottom:-2px">Structure</button>
        <button class="tab" data-tax-pane="tax-inheritance" style="padding:8px 16px;font-size:13px;color:#8b949e;background:transparent;border:none;border-bottom:2px solid transparent;cursor:pointer;margin-bottom:-2px">Inheritance</button>
        <button class="tab" data-tax-pane="tax-communities" style="padding:8px 16px;font-size:13px;color:#8b949e;background:transparent;border:none;border-bottom:2px solid transparent;cursor:pointer;margin-bottom:-2px">Communities</button>
        <span style="flex:1"></span>
        <input id="tax-search" type="text" placeholder="Search nodes..." style="padding:4px 8px;background:#161b22;color:#c9d1d9;border:1px solid #30363d;font-size:12px;width:160px;font-family:inherit">
        <span id="tax-stats" style="font-size:11px;color:#8b949e;margin-left:12px"></span>
      </div>
      <section class="emb-explainer" id="tax-explainer" aria-label="Taxonomy graph guide">
        <div class="emb-explainer__bar">
          <div class="emb-explainer__lede" id="tax-explainer-lede">
            <strong>Structure</strong> shows the containment map from repository and directories down to files and symbols, so you can trace where code lives before you drill into relationships.
          </div>
          <div class="emb-explainer__controls">
            <span class="emb-explainer__badge" id="tax-explainer-current">Structure</span>
            <button type="button" id="tax-explainer-toggle" class="emb-explainer__chevron" aria-expanded="true" aria-controls="tax-explainer-body" title="Hide tips"><span class="chev">&#9662;</span></button>
          </div>
        </div>
        <div class="emb-explainer__body" id="tax-explainer-body">
          <article class="emb-explainer__panel active" data-tax-explainer-panel="tax-structure">
            <span class="emb-explainer__eyebrow">Structure</span>
            <p><strong>Containment map.</strong> Read the repo as nested containers: repo → directories → files → symbols.</p>
            <p><strong>Use it for</strong> locating where a feature sits, checking file ownership, and narrowing depth before you inspect details.</p>
            <p><strong>Interaction</strong> Use the depth slider to peel layers back and tap a node to isolate its subtree.</p>
          </article>
          <article class="emb-explainer__panel" data-tax-explainer-panel="tax-inheritance">
            <span class="emb-explainer__eyebrow">Inheritance</span>
            <p><strong>Type hierarchy.</strong> This view shows `INHERITS` and `IMPLEMENTS` edges between classes, interfaces, traits, and structs.</p>
            <p><strong>Use it for</strong> spotting deep hierarchies, shared base types, and where interface contracts fan out into concrete implementations.</p>
            <p><strong>Interaction</strong> Tap a node to keep its immediate inheritance neighborhood bright while the rest of the graph fades back.</p>
          </article>
          <article class="emb-explainer__panel" data-tax-explainer-panel="tax-communities">
            <span class="emb-explainer__eyebrow">Communities</span>
            <p><strong>Semantic neighborhoods.</strong> Communities cluster symbols that are tightly connected structurally and semantically.</p>
            <p><strong>Use it for</strong> identifying feature slices, bridge-heavy modules, and cross-boundary seams that may need cleanup.</p>
            <p><strong>Interaction</strong> Compare community cards, cross-edge counts, and highlighted nodes to see which clusters are cohesive versus leaky.</p>
          </article>
        </div>
      </section>

      <!-- STRUCTURE SUB-TAB -->
      <div id="tax-structure" style="flex:1;position:relative">
        <div id="tax-structure-cy" style="position:absolute;inset:0;background:#0d1117"></div>
        <div style="position:absolute;bottom:16px;left:16px;display:flex;gap:12px;align-items:center;background:rgba(13,17,23,0.85);padding:6px 12px;border:1px solid #30363d;font-size:11px;color:#8b949e">
          <label>Depth <input type="range" id="tax-depth" min="1" max="5" value="3" style="width:80px;vertical-align:middle"> <span id="tax-depth-val">3</span></label>
        </div>
        <div id="tax-structure-legend" style="position:absolute;top:12px;right:12px;background:rgba(13,17,23,0.85);padding:8px 12px;border:1px solid #30363d;font-size:10px;color:#8b949e;display:flex;flex-direction:column;gap:2px"></div>
      </div>

      <!-- INHERITANCE SUB-TAB -->
      <div id="tax-inheritance" style="flex:1;position:relative;display:none">
        <div id="tax-inheritance-cy" style="position:absolute;inset:0;background:#0d1117"></div>
        <div id="tax-inh-stats" style="position:absolute;top:12px;right:12px;background:rgba(13,17,23,0.85);padding:8px 12px;border:1px solid #30363d;font-size:11px;color:#8b949e"></div>
      </div>

      <!-- COMMUNITIES SUB-TAB -->
      <div id="tax-communities" style="flex:1;position:relative;display:none">
        <div id="tax-communities-cy" style="position:absolute;inset:0;background:#0d1117"></div>
        <div id="tax-comm-stats" style="position:absolute;top:12px;right:12px;background:rgba(13,17,23,0.85);padding:8px 12px;border:1px solid #30363d;font-size:11px;color:#8b949e"></div>
        <div id="tax-comm-legend" style="position:absolute;top:12px;left:12px;background:rgba(13,17,23,0.85);padding:8px 12px;border:1px solid #30363d;font-size:10px;color:#8b949e;display:flex;flex-direction:column;gap:2px;max-height:40%;overflow-y:auto"></div>
        <div id="tax-comm-profiles" style="position:absolute;right:12px;bottom:12px;width:360px;max-width:calc(100% - 24px);max-height:52%;overflow-y:auto;background:rgba(13,17,23,0.9);border:1px solid #30363d;padding:10px 12px;display:flex;flex-direction:column;gap:10px"></div>
      </div>
    </div>

    <script>
    (function() {
      const taxonomyData = __TAXONOMY_JSON__;
      const TAX_COLORS = {
        Repository:'#f0883e', Directory:'#d29922', File:'#8b949e', Module:'#f778ba',
        Class:'#d2a8ff', Function:'#7ee787', Variable:'#79c0ff', Interface:'#58a6ff',
        Struct:'#f778ba', Enum:'#ff7b72', Trait:'#a5d6ff', Macro:'#ffa657',
        Union:'#ff7b72', Annotation:'#d2a8ff', Record:'#f778ba', Property:'#79c0ff',
      };
      const TAX_SIZES = {
        Repository:28, Directory:22, File:16, Module:14,
        Class:14, Function:10, Variable:8, Interface:14,
      };
      const TAX_EXPLAINER_COPY = {
        'tax-structure': {
          label: 'Structure',
          lede: '<strong>Structure</strong> shows the containment map from repository and directories down to files and symbols, so you can trace where code lives before you drill into relationships.',
        },
        'tax-inheritance': {
          label: 'Inheritance',
          lede: '<strong>Inheritance</strong> isolates the type hierarchy, making it easier to read parent-child contracts, implementations, and deep base-class chains.',
        },
        'tax-communities': {
          label: 'Communities',
          lede: '<strong>Communities</strong> groups nodes into semantic and structural neighborhoods so you can spot cohesive feature slices and leaky cross-boundary bridges.',
        },
      };

      function updateTaxonomyExplainer(paneId) {
        const meta = TAX_EXPLAINER_COPY[paneId] || TAX_EXPLAINER_COPY['tax-structure'];
        const ledeEl = document.getElementById('tax-explainer-lede');
        const currentEl = document.getElementById('tax-explainer-current');
        if (ledeEl) ledeEl.innerHTML = meta.lede;
        if (currentEl) currentEl.textContent = meta.label;
        document.querySelectorAll('[data-tax-explainer-panel]').forEach(panel => {
          panel.classList.toggle('active', panel.dataset.taxExplainerPanel === paneId);
        });
      }

      // ── Sub-tab switching ──
      document.querySelectorAll('[data-tax-pane]').forEach(btn => {
        btn.addEventListener('click', () => {
          document.querySelectorAll('[data-tax-pane]').forEach(b => {
            b.style.color='#8b949e'; b.style.borderBottomColor='transparent'; b.classList.remove('active');
          });
          btn.style.color='#58a6ff'; btn.style.borderBottomColor='#58a6ff'; btn.classList.add('active');
          document.getElementById('tax-structure').style.display = btn.dataset.taxPane==='tax-structure'?'block':'none';
          document.getElementById('tax-inheritance').style.display = btn.dataset.taxPane==='tax-inheritance'?'block':'none';
          document.getElementById('tax-communities').style.display = btn.dataset.taxPane==='tax-communities'?'flex':'none';
          updateTaxonomyExplainer(btn.dataset.taxPane);
        });
      });
      updateTaxonomyExplainer('tax-structure');

      // ── Lazy Cytoscape loader ──
      function loadScript(src, cb) {
        const s = document.createElement('script');
        s.src = src; s.onload = cb; document.head.appendChild(s);
      }

      function escapeTaxHtml(value) {
        return String(value ?? '').replace(/[&<>"']/g, ch => ({
          '&': '&amp;',
          '<': '&lt;',
          '>': '&gt;',
          '"': '&quot;',
          "'": '&#39;',
        }[ch]));
      }

      function initTaxonomyViews() {
        if (typeof cytoscapeDagre !== 'undefined') cytoscape.use(cytoscapeDagre);
        initStructureView();
        initInheritanceView();
        initCommunitiesView();

        const taxExplainer = document.getElementById('tax-explainer');
        const taxExplainerToggle = document.getElementById('tax-explainer-toggle');
        if (taxExplainer && taxExplainerToggle && !taxExplainer.dataset.boundToggle) {
          taxExplainerToggle.addEventListener('click', () => {
            const collapsed = taxExplainer.classList.toggle('collapsed');
            taxExplainerToggle.setAttribute('aria-expanded', String(!collapsed));
            taxExplainerToggle.setAttribute('title', collapsed ? 'Show tips' : 'Hide tips');
          });
          taxExplainer.dataset.boundToggle = 'true';
        }
      }

      // ── Structure view ──
      function initStructureView() {
        const data = taxonomyData && taxonomyData.structure;
        if (!data || !data.nodes || data.nodes.length === 0) {
          document.getElementById('tax-structure-cy').innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#8b949e">No structure data. Index a repository first.</div>';
          return;
        }

        const depthSlider = document.getElementById('tax-depth');
        const depthVal = document.getElementById('tax-depth-val');
        const statsEl = document.getElementById('tax-stats');
        const legendEl = document.getElementById('tax-structure-legend');

        // Compute depth per node
        const parentMap = {};
        data.nodes.forEach(n => { parentMap[n.id] = n.parent; });
        const depthCache = {};
        function nodeDepth(id) {
          if (depthCache[id] !== undefined) return depthCache[id];
          let d = 0, cur = id;
          while (parentMap[cur]) { d++; cur = parentMap[cur]; if (d > 10) break; }
          depthCache[id] = d;
          return d;
        }

        let maxDepth = parseInt(depthSlider.value);

        function buildElements() {
          const visible = data.nodes.filter(n => nodeDepth(n.id) <= maxDepth);
          const visibleIds = new Set(visible.map(n => n.id));
          return visible.map(n => ({
            data: {
              id: n.id, label: n.label, type: n.type,
              parent: (n.parent && visibleIds.has(n.parent)) ? n.parent : undefined,
              path: n.path, line: n.line,
            }
          }));
        }

        function updateStats() {
          const s = data.stats;
          const parts = Object.entries(s).map(([k,v]) => v + ' ' + k);
          statsEl.textContent = parts.join(' · ');
        }

        // Legend
        const seenTypes = new Set(data.nodes.map(n => n.type));
        legendEl.innerHTML = Array.from(seenTypes).sort().map(t =>
          '<div><span style="display:inline-block;width:8px;height:8px;background:' +
          (TAX_COLORS[t]||'#8b949e') + ';margin-right:6px"></span>' + t + '</div>'
        ).join('');

        const cy = cytoscape({
          container: document.getElementById('tax-structure-cy'),
          elements: buildElements(),
          style: [
            { selector: 'node', style: {
              'background-color': function(ele) { return TAX_COLORS[ele.data('type')] || '#8b949e'; },
              'label': 'data(label)', 'font-size': 9, 'color': '#8b949e',
              'text-halign': 'center', 'text-valign': 'bottom',
              'width': function(ele) { return TAX_SIZES[ele.data('type')] || 10; },
              'height': function(ele) { return TAX_SIZES[ele.data('type')] || 10; },
            }},
            { selector: ':parent', style: {
              'background-opacity': 0.06, 'border-width': 1,
              'border-color': function(ele) { return TAX_COLORS[ele.data('type')] || '#30363d'; },
              'text-valign': 'top', 'text-halign': 'center', 'font-size': 10,
              'color': function(ele) { return TAX_COLORS[ele.data('type')] || '#8b949e'; },
            }},
            { selector: '.faded', style: { opacity: 0.08 }},
            { selector: '.hit', style: { opacity: 1, 'z-index': 10 }},
          ],
          layout: { name: 'dagre', rankDir: 'TB', animate: false, spacingFactor: 1.1 },
          minZoom: 0.1, maxZoom: 4,
        });

        updateStats();

        depthSlider.addEventListener('input', function() {
          maxDepth = parseInt(this.value);
          depthVal.textContent = maxDepth;
          cy.json({ elements: buildElements() });
          cy.layout({ name: 'dagre', rankDir: 'TB', animate: false, spacingFactor: 1.1 }).run();
        });

        // Click → highlight subtree
        cy.on('tap', 'node', function(e) {
          cy.elements().removeClass('faded hit');
          cy.elements().addClass('faded');
          const target = e.target;
          const subtree = target.union(target.descendants());
          subtree.removeClass('faded').addClass('hit');
        });
        cy.on('tap', function(e) { if (e.target === cy) cy.elements().removeClass('faded hit'); });

        // Search
        const searchInput = document.getElementById('tax-search');
        searchInput.addEventListener('input', function() {
          const q = this.value.trim().toLowerCase();
          cy.elements().removeClass('faded hit');
          if (!q) return;
          const matches = cy.nodes().filter(n => (n.data('label')||'').toLowerCase().includes(q));
          if (matches.length === 0) return;
          cy.elements().addClass('faded');
          matches.union(matches.ancestors()).removeClass('faded').addClass('hit');
        });
      }

      // ── Inheritance view ──
      function initInheritanceView() {
        const data = taxonomyData && taxonomyData.inheritance;
        if (!data || !data.nodes || data.nodes.length === 0) {
          document.getElementById('tax-inheritance-cy').innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#8b949e">No inheritance relationships found.</div>';
          return;
        }

        const statsEl = document.getElementById('tax-inh-stats');
        const s = data.stats;
        statsEl.innerHTML = s.trees + ' tree(s) · ' + s.total_nodes + ' nodes · ' +
          s.inherits_edges + ' inherits · ' + s.implements_edges + ' implements';

        const elements = [
          ...data.nodes.map(n => ({
            data: { id: n.id, label: n.label, type: n.type, path: n.path, line: n.line }
          })),
          ...data.edges.map((e, i) => ({
            data: { id: 'inh-e-' + i, source: e.source, target: e.target, edgeType: e.type }
          })),
        ];

        const cy = cytoscape({
          container: document.getElementById('tax-inheritance-cy'),
          elements: elements,
          style: [
            { selector: 'node', style: {
              'background-color': function(ele) { return TAX_COLORS[ele.data('type')] || '#8b949e'; },
              'label': 'data(label)', 'font-size': 10, 'color': '#c9d1d9',
              'text-halign': 'center', 'text-valign': 'bottom',
              'width': 14, 'height': 14,
            }},
            { selector: 'edge', style: {
              'line-color': function(ele) { return ele.data('edgeType')==='INHERITS' ? '#d2a8ff' : '#58a6ff'; },
              'target-arrow-shape': 'triangle',
              'target-arrow-color': function(ele) { return ele.data('edgeType')==='INHERITS' ? '#d2a8ff' : '#58a6ff'; },
              'curve-style': 'straight', 'width': 2, 'opacity': 0.7,
            }},
            { selector: '.faded', style: { opacity: 0.08 }},
            { selector: '.hit', style: { opacity: 1, 'z-index': 10 }},
          ],
          layout: { name: 'dagre', rankDir: 'BT', animate: false, spacingFactor: 1.2 },
          minZoom: 0.1, maxZoom: 4,
        });

        cy.on('tap', 'node', function(e) {
          cy.elements().removeClass('faded hit');
          cy.elements().addClass('faded');
          e.target.closedNeighborhood().removeClass('faded').addClass('hit');
        });
        cy.on('tap', function(e) { if (e.target === cy) cy.elements().removeClass('faded hit'); });
      }

      // ── Communities view ──
      function initCommunitiesView() {
        const data = taxonomyData && taxonomyData.communities;
        if (!data || !data.communities || data.communities.length === 0) {
          document.getElementById('tax-communities-cy').innerHTML =
            '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#8b949e">' +
            'No community data. Run <code style="background:#161b22;padding:2px 6px;border:1px solid #30363d">kkg embed</code> first to enable semantic community detection.</div>';
          return;
        }

        const COMM_PALETTE = [
          '#7ee787','#58a6ff','#d2a8ff','#f0883e','#f778ba',
          '#ff7b72','#d29922','#a5d6ff','#79c0ff','#ffa657',
          '#8b949e','#7ee787','#58a6ff','#d2a8ff','#f0883e',
        ];

        const statsEl = document.getElementById('tax-comm-stats');
        const legendEl = document.getElementById('tax-comm-legend');
        const profilesEl = document.getElementById('tax-comm-profiles');
        const s = data.stats;
        const couplingRatio = s.total_edges > 0 ? (s.cross_community_edges / s.total_edges * 100).toFixed(1) : '0.0';
        statsEl.innerHTML = s.communities + ' communities &middot; ' + s.total_nodes + ' nodes &middot; ' +
          s.structural_edges + ' structural &middot; ' + s.semantic_edges + ' semantic &middot; ' +
          '<span style="color:'+(parseFloat(couplingRatio)>30?'#f85149':'#7ee787')+'">' +
          s.cross_community_edges + ' cross-boundary (' + couplingRatio + '% coupling)</span>';

        // Build community color map
        const nodeToComm = {};
        data.communities.forEach(c => {
          c.members.forEach(m => { nodeToComm[m.uid] = c.id; });
        });

        // Community size map for node scaling
        const commSizeMap = {};
        data.communities.forEach(c => { commSizeMap[c.id] = c.size; });
        const maxCommSize = Math.max(...data.communities.map(c => c.size), 1);

        // Per-community cross-edge counts
        const commCrossCount = {};
        (data.cross_edges || []).forEach(e => {
          commCrossCount[e.source_community] = (commCrossCount[e.source_community]||0) + 1;
          commCrossCount[e.target_community] = (commCrossCount[e.target_community]||0) + 1;
        });

        // Legend with coupling density
        legendEl.innerHTML = data.communities.map(c => {
          const cross = commCrossCount[c.id] || 0;
          const densityTag = cross > 0 ? ' <span style="color:#f0883e;font-size:10px">' + cross + ' cross</span>' : '';
          return '<div style="display:flex;align-items:center;gap:4px;margin-bottom:2px">' +
            '<span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:' +
            COMM_PALETTE[c.id % COMM_PALETTE.length] + '"></span>' +
            '<span>C' + c.id + '</span>' +
            '<span style="color:#8b949e;font-size:10px">(' + c.size + ' nodes)</span>' +
            densityTag + '</div>';
        }).join('');

        function renderCommunityProfiles() {
          if (!profilesEl) return;
          const cards = data.communities.map(c => {
            const profile = c.profile || {};
            const dominant = (profile.dominant_types || []).map(item =>
              '<span style="display:inline-flex;align-items:center;gap:4px;padding:2px 7px;border:1px solid #30363d;color:#c9d1d9;background:#161b22;font-size:10px">' +
              escapeTaxHtml(item.type) + ' <strong style="color:#58a6ff">' + escapeTaxHtml(item.count) + '</strong></span>'
            ).join('');
            const hotspots = (profile.hotspots || []).map(item =>
              '<div style="display:flex;justify-content:space-between;gap:8px;font-size:10px;color:#8b949e">' +
              '<code style="color:#9ecbff;background:none">' + escapeTaxHtml(item.path) + '</code>' +
              '<span>' + escapeTaxHtml(item.count) + '</span></div>'
            ).join('');
            const members = (profile.sample_members || []).map(item =>
              '<div style="font-size:10px;color:#8b949e;display:flex;justify-content:space-between;gap:8px">' +
              '<span><strong style="color:#e6edf3">' + escapeTaxHtml(item.name) + '</strong> <span style="color:#58a6ff">' + escapeTaxHtml(item.type) + '</span></span>' +
              '<code style="color:#6e7681;background:none">' + escapeTaxHtml(item.path) + '</code></div>'
            ).join('');
            const rationale = (profile.rationale || []).map(item =>
              '<div style="padding:8px 9px;border:1px solid #21262d;background:#0d1117">' +
              '<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">' +
              '<span style="display:inline-block;padding:1px 6px;background:#f0883e22;color:#f0883e;border:1px solid #f0883e55;font-size:10px;font-weight:600">' + escapeTaxHtml(item.tag) + '</span>' +
              '<strong style="color:#e6edf3;font-size:10px">' + escapeTaxHtml(item.symbol) + '</strong>' +
              '<span style="color:#6e7681;font-size:10px">' + escapeTaxHtml(item.path) + (item.line ? ':' + escapeTaxHtml(item.line) : '') + '</span>' +
              '</div>' +
              '<div style="font-size:11px;line-height:1.45;color:#c9d1d9">' + escapeTaxHtml(item.text) + '</div>' +
              '</div>'
            ).join('');
            const shapeColor = profile.shape === 'bridge-heavy' ? '#f85149' : '#7ee787';
            return '<article data-community-card="' + c.id + '" style="padding:10px;border:1px solid #30363d;background:#11161d;display:flex;flex-direction:column;gap:8px;cursor:pointer">' +
              '<div style="display:flex;align-items:center;gap:8px">' +
              '<span style="display:inline-block;width:11px;height:11px;border-radius:2px;background:' + COMM_PALETTE[c.id % COMM_PALETTE.length] + '"></span>' +
              '<strong style="color:#e6edf3">Community ' + c.id + '</strong>' +
              '<span style="font-size:10px;color:#8b949e">' + c.size + ' nodes</span>' +
              '<span style="margin-left:auto;font-size:10px;color:' + shapeColor + '">' + escapeTaxHtml(profile.shape || 'mixed') + '</span>' +
              '</div>' +
              '<div style="font-size:11px;line-height:1.45;color:#9ba6b3">' + escapeTaxHtml(profile.summary || 'Community summary unavailable.') + '</div>' +
              '<div style="display:flex;justify-content:space-between;font-size:10px;color:#8b949e">' +
              '<span>' + (profile.cross_edge_count || 0) + ' cross edges</span>' +
              '<span>' + ((profile.rationale || []).length) + ' WHY/HACK/NOTE hits</span></div>' +
              '<div><div style="font-size:10px;color:#58a6ff;margin-bottom:4px;text-transform:uppercase;letter-spacing:0.08em">Types</div>' +
              '<div style="display:flex;flex-wrap:wrap;gap:6px">' + (dominant || '<span style="font-size:10px;color:#6e7681">No typed members</span>') + '</div></div>' +
              '<div><div style="font-size:10px;color:#58a6ff;margin-bottom:4px;text-transform:uppercase;letter-spacing:0.08em">Hotspots</div>' +
              '<div style="display:flex;flex-direction:column;gap:3px">' + (hotspots || '<span style="font-size:10px;color:#6e7681">No file paths recorded</span>') + '</div></div>' +
              '<div><div style="font-size:10px;color:#58a6ff;margin-bottom:4px;text-transform:uppercase;letter-spacing:0.08em">Representative symbols</div>' +
              '<div style="display:flex;flex-direction:column;gap:3px">' + (members || '<span style="font-size:10px;color:#6e7681">No symbol samples yet</span>') + '</div></div>' +
              '<div><div style="font-size:10px;color:#58a6ff;margin-bottom:4px;text-transform:uppercase;letter-spacing:0.08em">Rationale comments (WHY/HACK/NOTE)</div>' +
              '<div style="display:flex;flex-direction:column;gap:6px">' + (rationale || '<div style="font-size:10px;color:#6e7681">No WHY/HACK/NOTE comments captured for this community yet.</div>') + '</div></div>' +
              '</article>';
          }).join('');
          profilesEl.innerHTML =
            '<div style="display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #21262d;padding-bottom:6px">' +
            '<strong style="color:#e6edf3;font-size:12px">Community profiles</strong>' +
            '<span style="font-size:10px;color:#8b949e">click a card or node to focus</span></div>' +
            cards;
        }

        // Build Cytoscape elements — nodes colored by community
        const nodeSet = new Set();
        const elements = [];
        data.communities.forEach(c => {
          c.members.forEach(m => {
            if (!nodeSet.has(m.uid)) {
              nodeSet.add(m.uid);
              elements.push({
                data: { id: m.uid, label: m.name, community: c.id, type: m.type, path: m.path }
              });
            }
          });
        });
        data.edges.forEach((e, i) => {
          if (nodeSet.has(e.source) && nodeSet.has(e.target)) {
            elements.push({
              data: {
                id: 'comm-e-' + i, source: e.source, target: e.target,
                edgeType: e.type, provenance: e.provenance, confidence: e.confidence,
              }
            });
          }
        });

        const cy = cytoscape({
          container: document.getElementById('tax-communities-cy'),
          elements: elements,
          style: [
            { selector: 'node', style: {
              'background-color': function(ele) {
                return COMM_PALETTE[ele.data('community') % COMM_PALETTE.length];
              },
              'label': 'data(label)', 'font-size': 9, 'color': '#8b949e',
              'text-halign': 'center', 'text-valign': 'bottom',
              'width': function(ele) {
                var s = commSizeMap[ele.data('community')] || 1;
                return 6 + Math.round(14 * s / maxCommSize);
              },
              'height': function(ele) {
                var s = commSizeMap[ele.data('community')] || 1;
                return 6 + Math.round(14 * s / maxCommSize);
              },
            }},
            { selector: 'edge', style: {
              'line-color': function(ele) {
                var src = nodeToComm[ele.data('source')];
                var tgt = nodeToComm[ele.data('target')];
                if (src !== undefined && tgt !== undefined && src !== tgt) return '#f85149';
                if (ele.data('provenance') === 'inferred') return '#d29922';
                return '#30363d';
              },
              'width': function(ele) {
                var src = nodeToComm[ele.data('source')];
                var tgt = nodeToComm[ele.data('target')];
                if (src !== undefined && tgt !== undefined && src !== tgt) return 2;
                return ele.data('provenance') === 'inferred' ? 1.5 : 1;
              },
              'opacity': function(ele) {
                var src = nodeToComm[ele.data('source')];
                var tgt = nodeToComm[ele.data('target')];
                if (src !== undefined && tgt !== undefined && src !== tgt) return 0.7;
                return 0.3;
              },
              'curve-style': 'straight',
            }},
            { selector: '.faded', style: { opacity: 0.06 }},
            { selector: '.hit', style: { opacity: 1, 'z-index': 10 }},
          ],
          layout: { name: 'cose', animate: false, nodeRepulsion: function() { return 8000; },
                    idealEdgeLength: function() { return 80; }, numIter: 500 },
          minZoom: 0.1, maxZoom: 4,
        });

        function setFocusedCommunity(comm) {
          cy.elements().removeClass('faded hit');
          cy.elements().addClass('faded');
          cy.nodes().filter(n => n.data('community') === comm).removeClass('faded').addClass('hit');
          cy.edges().filter(edge => {
            const src = nodeToComm[edge.data('source')];
            const tgt = nodeToComm[edge.data('target')];
            return src === comm || tgt === comm;
          }).removeClass('faded').addClass('hit');
          if (profilesEl) {
            profilesEl.querySelectorAll('[data-community-card]').forEach(card => {
              const active = Number(card.dataset.communityCard) === comm;
              card.style.borderColor = active ? '#58a6ff' : '#30363d';
              card.style.background = active ? '#161b22' : '#11161d';
            });
            const selected = profilesEl.querySelector('[data-community-card="' + comm + '"]');
            if (selected) selected.scrollIntoView({block:'nearest'});
          }
        }

        function clearFocusedCommunity() {
          cy.elements().removeClass('faded hit');
          if (profilesEl) {
            profilesEl.querySelectorAll('[data-community-card]').forEach(card => {
              card.style.borderColor = '#30363d';
              card.style.background = '#11161d';
            });
          }
        }

        renderCommunityProfiles();
        if (profilesEl) {
          profilesEl.querySelectorAll('[data-community-card]').forEach(card => {
            card.addEventListener('click', () => {
              setFocusedCommunity(Number(card.dataset.communityCard));
            });
          });
        }

        // Click community node → highlight its community
        cy.on('tap', 'node', function(e) {
          const comm = e.target.data('community');
          setFocusedCommunity(comm);
        });
        cy.on('tap', function(e) { if (e.target === cy) clearFocusedCommunity(); });
      }

      // ── Lazy init entry point ──
      window._taxInit = function() {
        if (typeof cytoscape === 'undefined') {
          loadScript('https://unpkg.com/cytoscape@3.28.1/dist/cytoscape.min.js', function() {
            loadScript('https://unpkg.com/dagre@0.8.5/dist/dagre.min.js', function() {
              loadScript('https://unpkg.com/cytoscape-dagre@2.5.0/cytoscape-dagre.js', function() {
                initTaxonomyViews();
              });
            });
          });
        } else {
          initTaxonomyViews();
        }
      };
    })();
    </script>
  </div>
</div>
<div class="modal-overlay" id="about-overlay" style="display:none">
  <div class="modal">
    <button type="button" class="modal-close" id="about-close">&times;</button>
    <h2>KeplerKG</h2>
    <h3>Purpose</h3>
    <p>KeplerKG exists to make the creation of knowledge graphs and embeddings for institutional knowledge of all kinds &mdash; code is the pilot domain, not the ceiling. The code-graph work is a beachhead; the generalised goal is turning any corpus (documentation, meeting transcripts, ticket histories, process wikis) into a navigable graph and embedding space that surfaces structure, similarity, and drift automatically.</p>
    <h3>Validated by Dogfooding</h3>
    <ul>
      <li><strong>67.4% token reduction</strong> &mdash; review-packet vs raw diff across 15 real commits</li>
      <li><strong>482x context compression</strong> &mdash; kkg search (~760 tokens) vs reading all files (366K tokens)</li>
      <li><strong>323 graph-exclusive findings</strong> &mdash; issues invisible to line-by-line tools (pylint, radon)</li>
    </ul>
    <p style="font-size:0.85em;color:#888;">Reproducible experiments: <code>research/experiments/dogfooding/</code></p>
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
__LOADING_JS__
</script>
<script>
const tabs = document.querySelectorAll("#tab-bar .tab");
const panes = document.querySelectorAll(".pane");

// Projector pane state.
const simpleBtn = document.getElementById("emb-simple-btn");
const advancedBtn = document.getElementById("emb-advanced-btn");
const embIframe = document.getElementById("emb-iframe");
let embLoaded = false;
let embAdvanced = false;
let stdLoaded = false;
let taxLoaded = false;

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
    if (tab.dataset.pane === "pane-standards" && !stdLoaded) {
      if (window._stdInit) window._stdInit();
      stdLoaded = true;
    }
    if (tab.dataset.pane === "pane-taxonomy" && !taxLoaded) {
      if (window._taxInit) window._taxInit();
      taxLoaded = true;
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

/* ── iframe load detection: fade out loading overlays ──────── */
(function() {
  var iframe2d = document.getElementById("iframe-2d");
  var iframe3d = document.getElementById("iframe-3d");
  var embIframeEl = document.getElementById("emb-iframe");

  function onIframeReady(iframe, paneId) {
    if (!iframe) return;
    /* srcdoc iframes fire load once populated */
    iframe.addEventListener("load", function() {
      if (window._kkgLoaded) window._kkgLoaded(paneId);
    });
  }
  onIframeReady(iframe2d, "pane-2d");
  onIframeReady(iframe3d, "pane-3d");
  onIframeReady(embIframeEl, "pane-embeddings");

  /* Standards: synchronous init — safe to dismiss loader immediately. */
  var origStdInit = window._stdInit;
  if (origStdInit) {
    window._stdInit = function() {
      origStdInit();
      if (window._kkgLoaded) window._kkgLoaded("pane-standards");
    };
  }
  /* Taxonomy: _taxInit loads Cytoscape scripts asynchronously, then calls
     initTaxonomyViews().  We must wait for initTaxonomyViews to actually
     run before dismissing the loader — not just _taxInit returning.

     The observer + fallback timer are NOT started at page load — they are
     deferred to when _taxInit is first invoked (i.e. when the user clicks
     the Taxonomy tab).  This prevents the 12s fallback from expiring on
     a tab the user never opened. */
  var origTaxInit = window._taxInit;
  if (origTaxInit) {
    window._taxInit = function() {
      origTaxInit();
      /* Now that _taxInit has been called, start watching for render. */
      var taxPane = document.getElementById("pane-taxonomy");
      if (!taxPane) { if (window._kkgLoaded) window._kkgLoaded("pane-taxonomy"); return; }
      var taxContainers = taxPane.querySelectorAll("[id$='-cy']");
      if (taxContainers.length === 0) { if (window._kkgLoaded) window._kkgLoaded("pane-taxonomy"); return; }
      var taxDismissed = false;
      var observer = new MutationObserver(function() {
        if (taxDismissed) return;
        for (var i = 0; i < taxContainers.length; i++) {
          if (taxContainers[i].querySelector("canvas")) {
            taxDismissed = true;
            observer.disconnect();
            if (window._kkgLoaded) window._kkgLoaded("pane-taxonomy");
            return;
          }
        }
      });
      for (var i = 0; i < taxContainers.length; i++) {
        observer.observe(taxContainers[i], { childList: true, subtree: true });
      }
      /* Fallback: if no canvas appears within 12s after tab activation
         (script load failure, empty data, etc.), dismiss anyway. */
      setTimeout(function() {
        if (!taxDismissed) {
          taxDismissed = true;
          observer.disconnect();
          if (window._kkgLoaded) window._kkgLoaded("pane-taxonomy");
        }
      }, 12000);
    };
  }
})();
</script>
</body>
</html>"""


def _dashboard_html(
    graph: dict[str, Any],
    emb_count: int,
    standards_json: str,
    violations_json: str = "[]",
    taxonomy_json: str = "null",
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
    out = out.replace("__STANDARDS_JSON__", standards_json)
    out = out.replace("__VIOLATIONS_JSON__", violations_json)
    out = out.replace("__TAXONOMY_JSON__", taxonomy_json)

    # Loading animation overlays
    out = out.replace("__LOADING_CSS__", LOADING_CSS)
    out = out.replace("__LOADING_JS__", LOADING_JS)
    out = out.replace("__LOADER_2D__", loader_html("pane-2d"))
    out = out.replace("__LOADER_3D__", loader_html("pane-3d"))
    out = out.replace("__LOADER_EMB__", loader_html("pane-embeddings", "Loading embeddings\u2026"))
    out = out.replace("__LOADER_STD__", loader_html("pane-standards", "Analyzing standards\u2026"))
    out = out.replace("__LOADER_TAX__", loader_html("pane-taxonomy", "Building taxonomy\u2026"))
    return out


def _load_standards_json() -> str:
    """Load standards rules as JSON for the dashboard Standards tab."""
    try:
        from ..standards.loader import load_rules
        from ..commands.audit import _find_standards_dir
        rules = load_rules(_find_standards_dir())
        return json.dumps([
            {
                "id": r.id,
                "category": r.category,
                "severity": r.severity,
                "summary": r.summary,
                "suggestion": r.suggestion,
                "evidence": r.evidence,
                "principle": _rule_principle(r.id, r.category),
                "thresholds": r.thresholds,
            }
            for r in rules
        ])
    except Exception:
        return "[]"


def _run_audit_for_viz(conn: Any) -> str:
    """Run audit against the live graph and return violations as JSON."""
    try:
        from ..standards.loader import load_rules, load_exemptions, run_rule
        from ..commands.audit import _find_standards_dir
        std_dir = _find_standards_dir()
        rules = load_rules(std_dir)
        exemptions = load_exemptions(std_dir)
        violations = []
        for rule in rules:
            result = run_rule(conn, rule, exemptions)
            if result.fired:
                adv = result.to_advisory()
                violations.append(adv)
        return json.dumps(violations)
    except Exception:
        return "[]"


_RATIONALE_TABLES = {
    "Annotation",
    "Class",
    "Enum",
    "Function",
    "Interface",
    "Macro",
    "Property",
    "Record",
    "Struct",
    "Trait",
    "Union",
    "Variable",
}
_RATIONALE_LINE_RE = re.compile(
    r"^\s*(?:[#/*;!-]+\s*)?(WHY|HACK|NOTE)\s*[:\-]\s*(.+?)\s*$",
    re.IGNORECASE,
)


def _short_path(path: str, *, parts: int = 2) -> str:
    """Return a compact tail segment for long repo paths."""
    raw = (path or "").replace("\\", "/").strip()
    if not raw:
        return "unknown"
    chunks = [chunk for chunk in raw.split("/") if chunk]
    if len(chunks) <= parts:
        return "/".join(chunks)
    return "/".join(chunks[-parts:])


def _truncate_text(text: str, *, max_chars: int = 160) -> str:
    """Collapse whitespace and cap long rationale snippets."""
    compact = " ".join((text or "").split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3].rstrip() + "..."


def _extract_rationale_comments(text: str, *, max_items: int = 6) -> list[dict[str, str]]:
    """Extract WHY/HACK/NOTE comments from source or docstring text."""
    if not text:
        return []

    notes: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip().strip("*/").strip()
        match = _RATIONALE_LINE_RE.match(line)
        if not match:
            continue
        tag = match.group(1).upper()
        snippet = _truncate_text(match.group(2).strip().strip("*/").strip())
        if not snippet:
            continue
        key = (tag, snippet.lower())
        if key in seen:
            continue
        seen.add(key)
        notes.append({"tag": tag, "text": snippet})
        if len(notes) >= max_items:
            break
    return notes


def _fetch_symbol_context(conn: Any, member: dict[str, Any]) -> dict[str, Any] | None:
    """Fetch source/docstring fields for a community member when available."""
    table = member.get("type")
    uid = member.get("uid")
    if table not in _RATIONALE_TABLES or not uid:
        return None

    query = (
        f"MATCH (n:`{table}`) WHERE n.uid = $uid "
        "RETURN n.name, n.path, n.line_number, n.docstring, n.source LIMIT 1"
    )
    try:
        result = conn.execute(query, uid=uid)
    except Exception:
        return None
    if not result.has_next():
        return None

    row = result.get_next()
    return {
        "name": row[0] or member.get("name") or uid,
        "path": row[1] or member.get("path") or "",
        "line": row[2] or member.get("line") or 0,
        "docstring": row[3] or "",
        "source": row[4] or "",
    }


def _build_community_profile(
    conn: Any,
    community: dict[str, Any],
    *,
    cross_edge_count: int,
) -> dict[str, Any]:
    """Summarize a Louvain community for the taxonomy profile rail."""
    members = sorted(
        community.get("members", []),
        key=lambda item: (
            str(item.get("type") or ""),
            str(item.get("path") or ""),
            str(item.get("name") or ""),
        ),
    )
    type_counts = Counter(str(member.get("type") or "unknown") for member in members)
    path_counts = Counter(
        _short_path(str(member.get("path") or ""))
        for member in members
        if member.get("path")
    )

    dominant_types = [
        {"type": item_type, "count": count}
        for item_type, count in type_counts.most_common(3)
    ]
    hotspots = [
        {"path": item_path, "count": count}
        for item_path, count in path_counts.most_common(3)
    ]
    sample_members = [
        {
            "name": member.get("name") or "(anonymous)",
            "type": member.get("type") or "unknown",
            "path": _short_path(str(member.get("path") or "")),
        }
        for member in members[:4]
    ]

    rationale: list[dict[str, Any]] = []
    seen_rationale: set[tuple[str, str, str]] = set()
    for member in members[:12]:
        context = _fetch_symbol_context(conn, member)
        if context is None:
            continue
        note_source = "\n".join(
            part for part in (context["docstring"], context["source"]) if part
        )
        for note in _extract_rationale_comments(note_source, max_items=4):
            key = (note["tag"], note["text"].lower(), context["name"])
            if key in seen_rationale:
                continue
            seen_rationale.add(key)
            rationale.append(
                {
                    "tag": note["tag"],
                    "text": note["text"],
                    "symbol": context["name"],
                    "path": _short_path(context["path"]),
                    "line": context["line"],
                }
            )
            if len(rationale) >= 4:
                break
        if len(rationale) >= 4:
            break

    primary_type = dominant_types[0]["type"] if dominant_types else "mixed"
    primary_hotspot = hotspots[0]["path"] if hotspots else "mixed paths"
    size = int(community.get("size") or len(members))
    summary = (
        f"{primary_type} cluster centered on {primary_hotspot} with "
        f"{cross_edge_count} cross-community edge(s)."
    )

    return {
        "summary": summary,
        "cross_edge_count": cross_edge_count,
        "dominant_types": dominant_types,
        "hotspots": hotspots,
        "sample_members": sample_members,
        "rationale": rationale,
        "shape": "bridge-heavy" if cross_edge_count >= max(size, 1) else "contained",
    }


def _annotate_taxonomy_profiles(data: dict[str, Any], conn: Any) -> dict[str, Any]:
    """Attach profile-card metadata to community records in taxonomy data."""
    community_payload = data.get("communities")
    if not isinstance(community_payload, dict):
        return data

    comm_list = community_payload.get("communities")
    if not isinstance(comm_list, list):
        return data

    cross_counts: Counter[int] = Counter()
    for edge in community_payload.get("cross_edges", []):
        src = edge.get("source_community")
        dst = edge.get("target_community")
        if isinstance(src, int):
            cross_counts[src] += 1
        if isinstance(dst, int):
            cross_counts[dst] += 1

    for community in comm_list:
        comm_id = int(community.get("id", -1))
        community["profile"] = _build_community_profile(
            conn,
            community,
            cross_edge_count=cross_counts.get(comm_id, 0),
        )
    return data


def _load_taxonomy_json(conn: Any, *, limit: int) -> str:
    """Fetch taxonomy data and enrich community payloads for the dashboard."""
    from .viz_taxonomy import fetch_taxonomy_data

    print("Fetching taxonomy data...", file=sys.stderr)
    data = fetch_taxonomy_data(conn, limit=limit)
    _annotate_taxonomy_profiles(data, conn)

    structure_nodes = len(data.get("structure", {}).get("nodes", []))
    inheritance_nodes = data.get("inheritance", {}).get("stats", {}).get(
        "total_nodes", 0
    )
    community_payload = data.get("communities") or {}
    community_count = community_payload.get("stats", {}).get("communities", 0)
    print(
        f"  taxonomy: {structure_nodes} structure nodes, "
        f"{inheritance_nodes} inheritance nodes, "
        f"{community_count} communities",
        file=sys.stderr,
    )
    return json.dumps(data)


def _copy_projector_bundle(dest: Path) -> None:
    """Stage projector assets even when importlib resources lacks a file origin."""
    try:
        copy_vendored_projector(dest)
        return
    except Exception:
        vendor_root = Path(__file__).resolve().parents[1] / "viz_assets" / "projector"
        dest.mkdir(parents=True, exist_ok=True)
        for name in VENDOR_FILES:
            shutil.copy2(vendor_root / name, dest / name)


def _prepare_dashboard_serve_dir(
    graph: dict[str, Any],
    emb_nodes: list[dict[str, Any]],
    *,
    layout: str,
    limit: int = 500,
) -> Path:
    """Create a tempdir with dashboard index.html + projector/ subdir.

    Extracted from the command body so tests can verify the layout without
    starting a server.
    """
    serve_dir = Path(tempfile.mkdtemp(prefix="cgraph-dashboard-"))

    standards_json = _load_standards_json()
    # Audit runs best-effort — needs a live DB connection
    violations_json = "[]"
    try:
        print("Running audit for standards tab...", file=sys.stderr)
        audit_conn = get_kuzu_connection()
        violations_json = _run_audit_for_viz(audit_conn)
    except Exception:
        pass

    tax_json = "null"
    try:
        tax_conn = get_kuzu_connection()
        tax_json = _load_taxonomy_json(tax_conn, limit=limit)
    except Exception:
        pass

    html = _dashboard_html(
        graph,
        len(emb_nodes),
        standards_json,
        violations_json,
        tax_json,
        layout=layout,
    )
    (serve_dir / "index.html").write_text(html, encoding="utf-8")

    projector_dir = serve_dir / "projector"
    _copy_projector_bundle(projector_dir)
    write_projector_data(projector_dir / DATA_SUBDIR, emb_nodes)

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
    project: Optional[str] = typer.Option(
        None,
        "--project",
        help=PROJECT_OPTION_HELP,
    ),
) -> None:
    """Unified dashboard: 2D graph, 3D graph, and TF Embedding Projector.

    Starts a local HTTP server; blocks until Ctrl-C.  Needed because the
    Embeddings tab fetches its config via real HTTP (can't be srcdoc-inlined).
    """

    target = activate_project(project)
    previous_runtime_db = os.environ.get("CGC_RUNTIME_DB_TYPE")
    # Project-aware dashboard invocations always target the activated Kuzu store,
    # even when the caller's shell has a different global DEFAULT_DATABASE.
    os.environ["CGC_RUNTIME_DB_TYPE"] = "kuzudb"

    try:
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

        serve_dir = _prepare_dashboard_serve_dir(
            graph,
            emb_nodes,
            layout=layout,
            limit=limit,
        )
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
            "project": target.slug,
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
    finally:
        if previous_runtime_db is None:
            os.environ.pop("CGC_RUNTIME_DB_TYPE", None)
        else:
            os.environ["CGC_RUNTIME_DB_TYPE"] = previous_runtime_db
