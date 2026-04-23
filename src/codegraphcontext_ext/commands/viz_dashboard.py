"""kkg viz-dashboard: server-backed multi-tab visualization dashboard.

Primary tabs in one browser window:
  1. 2D Graph   — Cytoscape.js (srcdoc iframe)
  2. 3D Graph   — 3d-force-graph (srcdoc iframe)
  3. Embeddings — TF Embedding Projector (iframe src="projector/")
  4. Standards  — live graph-backed standards configuration + violations

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
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

import typer

from ..embeddings.fetch import fetch_embedded_nodes
from ..embeddings.schema import EMBEDDABLE_TABLES, EMBEDDING_COLUMN
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
    _NODE_TABLES,
    _REL_QUERIES,
    _fetch_graph,
    _generate_html as _generate_graph_html,
)

COMMAND_NAME = "viz-dashboard"
SCHEMA_FILE = "context.json"
SUMMARY = "Unified dashboard: 2D graph, 3D graph, embeddings, and standards as tabs."

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


def _close_kuzu_connection() -> None:
    """Release the upstream Kuzu singleton once dashboard data is materialized.

    FalkorDB uses a different manager path and does not currently need an
    explicit close here; the helper remains Kuzu-specific on purpose.
    """
    try:
        from codegraphcontext.core.database_kuzu import KuzuDBManager

        KuzuDBManager().close_driver()
    except Exception:
        pass


def _human_join(parts: tuple[str, ...]) -> str:
    """Render a short human-readable list for UI copy."""
    if not parts:
        return "none"
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return f"{', '.join(parts[:-1])}, and {parts[-1]}"


def _query_single_count(conn: Any, query: str) -> int | None:
    """Run a count query and return the integer result when available."""
    try:
        result = conn.execute(query)
    except Exception:
        return None
    try:
        if not result.has_next():
            return 0
        row = result.get_next()
    except Exception:
        return None

    value: Any
    if isinstance(row, dict):
        value = next(iter(row.values()), 0)
    elif isinstance(row, (list, tuple)):
        value = row[0] if row else 0
    else:
        value = row

    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return None


def _sum_known_counts(counts: dict[str, int | None]) -> int | None:
    """Sum count values only when every entry is known."""
    if not counts or any(value is None for value in counts.values()):
        return None
    return sum(int(value) for value in counts.values() if value is not None)


def _format_count(value: int | None) -> str:
    """Render count text for dashboard placeholders."""
    return str(value) if value is not None else "Unavailable"


def _format_breakdown(counts: dict[str, int | None]) -> str:
    """Render a one-line breakdown string for dashboard placeholders."""
    if not counts or any(value is None for value in counts.values()):
        return "Unavailable"
    return " · ".join(f"{label} {value}" for label, value in counts.items())


def _format_coverage(stored: int, total: int | None) -> str:
    """Render embedding coverage text against the embeddable total."""
    if total is None or total <= 0:
        return "Unavailable"
    return f"{stored} / {total} ({(stored / total) * 100:.1f}%)"


def _collect_dashboard_count_details(conn: Any) -> dict[str, Any]:
    """Collect full visualization-scope totals and embedding coverage inputs."""
    full_node_counts = {
        table: _query_single_count(conn, f"MATCH (n:`{table}`) RETURN count(n)")
        for table in _NODE_TABLES
    }
    full_edge_counts = {
        rel_name: _query_single_count(conn, f"MATCH ()-[r:{rel_name}]->() RETURN count(r)")
        for rel_name, _query in _REL_QUERIES
    }
    embeddable_node_counts = {
        table: _query_single_count(conn, f"MATCH (n:`{table}`) RETURN count(n)")
        for table in EMBEDDABLE_TABLES
    }
    embedding_counts = {
        table: _query_single_count(
            conn,
            f"MATCH (n:`{table}`) WHERE n.`{EMBEDDING_COLUMN}` IS NOT NULL RETURN count(n)",
        )
        for table in EMBEDDABLE_TABLES
    }

    return {
        "full_node_counts": full_node_counts,
        "full_node_total": _sum_known_counts(full_node_counts),
        "full_edge_counts": full_edge_counts,
        "full_edge_total": _sum_known_counts(full_edge_counts),
        "embeddable_node_counts": embeddable_node_counts,
        "embeddable_total": _sum_known_counts(embeddable_node_counts),
        "embedding_counts": embedding_counts,
        "stored_embedding_total": _sum_known_counts(embedding_counts),
    }


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
  #nav .stats, .stats-shell, .stats-panel { font-family: "Antic", "Antic Slab", Georgia, sans-serif; letter-spacing: 0.02em; }
  .emb-explainer__body h3 { font-family: "Antic Didone", "Antic Slab", Georgia, serif;
                            letter-spacing: 0.12em; }
  .emb-explainer__body kbd { font-family: "Antic", "Antic Slab", Georgia, sans-serif; }
  /* Square every control: no rounded corners anywhere in the dashboard chrome. */
  .tab, .emb-explainer__controls button, .emb-explainer__chevron,
  .emb-explainer__body kbd { border-radius: 0 !important; }
  #nav { padding: 12px 24px; border-bottom: 1px solid #30363d; display: flex; align-items: center; gap: 24px; flex-shrink: 0; flex-wrap: wrap; }
  #nav h1 { font-size: 16px; font-weight: 600; color: #c9d1d9; }
  #nav .stats { font-size: 12px; color: #8b949e; margin-right: auto; }
  .stats-shell { display: flex; flex-wrap: wrap; gap: 6px; margin-right: auto; }
  .stats-pill {
    display: inline-flex; align-items: baseline; gap: 6px;
    padding: 6px 10px; font-size: 12px; color: #9ba6b3; background: #11161d;
    border: 1px solid #30363d; cursor: pointer; transition: color 0.12s, border-color 0.12s, background 0.12s;
  }
  .stats-pill:hover { color: #e6edf3; border-color: #58a6ff; background: #161b22; }
  .stats-pill.active { color: #58a6ff; border-color: #58a6ff; background: #161b22; }
  .stats-pill__value { font-size: 13px; color: #e6edf3; }
  .stats-pill.active .stats-pill__value { color: inherit; }
  .stats-pill__label { text-transform: lowercase; }
  .stats-panel {
    flex-shrink: 0; border-bottom: 1px solid #30363d; background: linear-gradient(180deg, #11161d 0%, #0d1117 100%);
  }
  .stats-panel[hidden] { display: none !important; }
  .stats-panel__bar {
    display: flex; align-items: center; gap: 12px; padding: 10px 24px 8px 24px;
    border-bottom: 1px solid #21262d;
  }
  .stats-panel__lede { flex: 1; font-size: 12px; line-height: 1.5; color: #e6edf3; }
  .stats-panel__lede strong { color: #58a6ff; font-weight: 600; }
  .stats-panel__close {
    width: 26px; height: 26px; padding: 0; border: 1px solid #30363d; background: transparent;
    color: #9ba6b3; cursor: pointer; transition: color 0.12s, border-color 0.12s, background 0.12s;
  }
  .stats-panel__close:hover { color: #e6edf3; border-color: #58a6ff; background: #161b22; }
  .stats-panel__body {
    display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; padding: 12px 24px 14px 24px;
  }
  .stats-card {
    padding: 12px; border: 1px solid #21262d; background: #11161d; color: #9ba6b3;
    transition: border-color 0.12s, background 0.12s, box-shadow 0.12s;
  }
  .stats-card.active { border-color: #58a6ff; background: #161b22; box-shadow: inset 0 0 0 1px rgba(88,166,255,0.22); }
  .stats-card__eyebrow {
    display: inline-block; margin-bottom: 8px; font-size: 10px; text-transform: uppercase;
    letter-spacing: 0.1em; color: #58a6ff;
  }
  .stats-card__metric { margin-bottom: 8px; font-size: 17px; color: #e6edf3; }
  .stats-card p { margin: 0 0 8px 0; font-size: 11px; line-height: 1.55; }
  .stats-card p:last-child { margin-bottom: 0; }
  .stats-card code, .stats-panel__footnote code {
    font-family: ui-monospace, SFMono-Regular, monospace; font-size: 10px;
    background: #0d1117; border: 1px solid #30363d; padding: 1px 4px; color: #e6edf3;
  }
  .stats-panel__footnote {
    padding: 0 24px 14px 24px; font-size: 11px; line-height: 1.55; color: #9ba6b3;
  }
  .tab-bar { display: flex; gap: 4px; flex-wrap: wrap; }
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
  @media (max-width: 1100px) {
    .stats-panel__body { grid-template-columns: 1fr; }
  }
__LOADING_CSS__
</style>
</head>
<body>
<div id="nav">
  <h1>KeplerKG</h1>
  <select id="project-switcher" title="Switch project" style="background:#1c2128;color:#c9d1d9;border:1px solid #30363d;padding:4px 8px;font-size:12px;font-family:inherit;cursor:pointer;margin-left:8px;">
    <option value="">__CURRENT_PROJECT__</option>
  </select>
  <div class="stats-shell" id="stats-shell" aria-label="Dashboard count details">
    <button type="button" class="stats-pill" data-stat-target="graph-nodes" aria-controls="stats-panel" aria-expanded="false">
      <span class="stats-pill__value">__NODE_COUNT__</span>
      <span class="stats-pill__label">nodes</span>
    </button>
    <button type="button" class="stats-pill" data-stat-target="graph-edges" aria-controls="stats-panel" aria-expanded="false">
      <span class="stats-pill__value">__EDGE_COUNT__</span>
      <span class="stats-pill__label">edges</span>
    </button>
    <button type="button" class="stats-pill" data-stat-target="embeddings" aria-controls="stats-panel" aria-expanded="false">
      <span class="stats-pill__value">__EMB_COUNT__</span>
      <span class="stats-pill__label">embeddings</span>
    </button>
  </div>
  <div class="tab-bar" id="tab-bar">
    <button class="tab active" data-pane="pane-2d">2D Graph</button>
    <button class="tab" data-pane="pane-3d">3D Graph</button>
    <button class="tab" data-pane="pane-embeddings">Embeddings</button>
    <button class="tab" data-pane="pane-standards">Standards</button>
  </div>
  <button type="button" id="about-btn">About</button>
</div>
<section class="stats-panel" id="stats-panel" aria-label="Dashboard count details" hidden>
  <div class="stats-panel__bar">
    <div class="stats-panel__lede">
      <strong>These counts come from different slices of the project.</strong> The 2D and 3D graph tabs use a preview fetch for responsiveness, while the Embeddings tab loads every stored vector for embeddable symbols. Click any count again to collapse this panel.
    </div>
    <button type="button" class="stats-panel__close" id="stats-panel-close" aria-label="Close count details">&times;</button>
  </div>
  <div class="stats-panel__body" id="stats-panel-body">
    <article class="stats-card active" data-stat-card="graph-nodes">
      <span class="stats-card__eyebrow">Graph Preview Nodes</span>
      <div class="stats-card__metric">__NODE_COUNT__ rendered nodes</div>
      <p>The 2D and 3D graph panes render the nodes fetched into the preview graph, not the full repository total.</p>
      <p>Full visualization-scope total: <code>__FULL_NODE_TOTAL__</code>. Type breakdown: <code>__FULL_NODE_BREAKDOWN__</code>.</p>
      <p>Dashboard launch currently uses <code>--limit __GRAPH_LIMIT__</code> per node table, so the preview can top out well below the full graph size.</p>
    </article>
    <article class="stats-card" data-stat-card="graph-edges">
      <span class="stats-card__eyebrow">Graph Preview Edges</span>
      <div class="stats-card__metric">__EDGE_COUNT__ rendered edges</div>
      <p>Edges count only when both endpoints survived the preview fetch and the relationship query returned them.</p>
      <p>Full visualization-scope total: <code>__FULL_EDGE_TOTAL__</code>. Relationship breakdown: <code>__FULL_EDGE_BREAKDOWN__</code>.</p>
      <p>Each relation query also uses <code>--limit __GRAPH_LIMIT__</code>, so this is a rendered edge count, not a global edge total.</p>
    </article>
    <article class="stats-card" data-stat-card="embeddings">
      <span class="stats-card__eyebrow">Embedding Dataset</span>
      <div class="stats-card__metric">__EMB_COUNT__ stored embeddings</div>
      <p>The Embeddings tab loads every non-null vector for embeddable symbol types: <code>__EMB_TYPES__</code>.</p>
      <p>Embeddable symbols in graph scope: <code>__EMBEDDABLE_TOTAL__</code>. Graph-side breakdown: <code>__EMBEDDABLE_BREAKDOWN__</code>.</p>
      <p>Stored vector breakdown: <code>__EMBEDDING_BREAKDOWN__</code>. Coverage: <code>__EMBEDDING_COVERAGE__</code>.</p>
      <p>This fetch is not capped by the graph preview limit, so it can exceed the rendered node count. It can also be lower if some embeddable symbols have not been embedded yet.</p>
    </article>
  </div>
  <div class="stats-panel__footnote">
    Why the numbers differ: graph counts are preview-limited for fast rendering, while embedding counts reflect stored vectors for embeddable symbols only. They describe related datasets, but they are not meant to be numerically identical.
  </div>
</section>
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
const statsPanel = document.getElementById("stats-panel");
const statsClose = document.getElementById("stats-panel-close");
const statButtons = document.querySelectorAll("[data-stat-target]");
const statCards = document.querySelectorAll("[data-stat-card]");
let embLoaded = false;
let embAdvanced = false;
let stdLoaded = false;
let activeStatTarget = "graph-nodes";

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

function syncStatDetails(target) {
  activeStatTarget = target;
  statButtons.forEach(btn => {
    const active = btn.dataset.statTarget === target && !statsPanel.hidden;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-expanded", String(active));
  });
  statCards.forEach(card => {
    card.classList.toggle("active", card.dataset.statCard === target);
  });
}

function closeStatsPanel() {
  statsPanel.hidden = true;
  syncStatDetails(activeStatTarget);
}

function openStatsPanel(target) {
  statsPanel.hidden = false;
  syncStatDetails(target);
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
  });
});

statButtons.forEach(btn => {
  btn.addEventListener("click", () => {
    const target = btn.dataset.statTarget;
    if (!statsPanel.hidden && activeStatTarget === target) {
      closeStatsPanel();
      return;
    }
    openStatsPanel(target);
  });
});

statsClose.addEventListener("click", closeStatsPanel);

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
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !statsPanel.hidden) closeStatsPanel();
});

/* ── project switcher ──────── */
(function() {
  const sel = document.getElementById("project-switcher");
  if (!sel) return;
  fetch("/api/projects")
    .then(r => r.json())
    .then(projects => {
      sel.innerHTML = "";
      projects.forEach(p => {
        const opt = document.createElement("option");
        opt.value = p.slug;
        opt.textContent = p.slug + (p.current ? " (active)" : "") + " — " + p.size_mb + " MB";
        if (p.current) opt.selected = true;
        sel.appendChild(opt);
      });
      sel.addEventListener("change", () => {
        const slug = sel.value;
        const msg = "To switch to " + slug + ", restart with:\\n\\nkkg viz-dashboard --project " + slug + "\\n\\nCopy to clipboard?";
        if (confirm(msg)) {
          navigator.clipboard.writeText("kkg viz-dashboard --project " + slug).catch(() => {});
        }
      });
    })
    .catch(() => { sel.style.display = "none"; });
})();

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
})();
</script>
</body>
</html>"""


def _dashboard_html(
    graph: dict[str, Any],
    emb_count: int,
    standards_json: str,
    violations_json: str = "[]",
    *,
    layout: str,
    project_slug: str = "default",
    graph_limit: int = 500,
    count_details: Optional[dict[str, Any]] = None,
) -> str:
    """Build the dashboard chrome.  The Embeddings tab iframes to `projector/`,
    which is served as a sibling directory by the HTTP server."""

    inner_2d = _generate_graph_html(graph, layout=layout, three_d=False)
    inner_3d = _generate_graph_html(graph, layout=layout, three_d=True)

    iframe_2d = _html.escape(inner_2d, quote=True)
    iframe_3d = _html.escape(inner_3d, quote=True)
    count_details = count_details or {}
    full_node_counts = dict(count_details.get("full_node_counts") or {})
    full_edge_counts = dict(count_details.get("full_edge_counts") or {})
    embeddable_node_counts = dict(count_details.get("embeddable_node_counts") or {})
    embedding_counts = dict(count_details.get("embedding_counts") or {})
    embeddable_total = count_details.get("embeddable_total")

    out = _DASHBOARD_TEMPLATE
    out = out.replace("__CURRENT_PROJECT__", project_slug)
    out = out.replace("__NODE_COUNT__", str(len(graph["nodes"])))
    out = out.replace("__EDGE_COUNT__", str(len(graph["edges"])))
    out = out.replace("__EMB_COUNT__", str(emb_count))
    out = out.replace("__GRAPH_LIMIT__", str(graph_limit))
    out = out.replace(
        "__EMB_TYPES__",
        _human_join(tuple(str(part) for part in EMBEDDABLE_TABLES)),
    )
    out = out.replace("__FULL_NODE_TOTAL__", _format_count(count_details.get("full_node_total")))
    out = out.replace("__FULL_NODE_BREAKDOWN__", _format_breakdown(full_node_counts))
    out = out.replace("__FULL_EDGE_TOTAL__", _format_count(count_details.get("full_edge_total")))
    out = out.replace("__FULL_EDGE_BREAKDOWN__", _format_breakdown(full_edge_counts))
    out = out.replace("__EMBEDDABLE_TOTAL__", _format_count(embeddable_total))
    out = out.replace("__EMBEDDABLE_BREAKDOWN__", _format_breakdown(embeddable_node_counts))
    out = out.replace("__EMBEDDING_BREAKDOWN__", _format_breakdown(embedding_counts))
    out = out.replace("__EMBEDDING_COVERAGE__", _format_coverage(emb_count, embeddable_total))
    out = out.replace("__IFRAME_2D__", iframe_2d)
    out = out.replace("__IFRAME_3D__", iframe_3d)
    out = out.replace("__STANDARDS_JSON__", standards_json)
    out = out.replace("__VIOLATIONS_JSON__", violations_json)

    # Loading animation overlays
    out = out.replace("__LOADING_CSS__", LOADING_CSS)
    out = out.replace("__LOADING_JS__", LOADING_JS)
    out = out.replace("__LOADER_2D__", loader_html("pane-2d"))
    out = out.replace("__LOADER_3D__", loader_html("pane-3d"))
    out = out.replace("__LOADER_EMB__", loader_html("pane-embeddings", "Loading embeddings\u2026"))
    out = out.replace("__LOADER_STD__", loader_html("pane-standards", "Analyzing standards\u2026"))
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
    project_slug: str = "default",
    count_details: Optional[dict[str, Any]] = None,
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

    html = _dashboard_html(
        graph,
        len(emb_nodes),
        standards_json,
        violations_json,
        layout=layout,
        project_slug=project_slug,
        graph_limit=limit,
        count_details=count_details,
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
    kuzu_released = False

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
        count_details = _collect_dashboard_count_details(conn)

        serve_dir = _prepare_dashboard_serve_dir(
            graph,
            emb_nodes,
            layout=layout,
            limit=limit,
            project_slug=target.slug,
            count_details=count_details,
        )
        _close_kuzu_connection()
        kuzu_released = True
        bound_port = find_free_port(port or None)
        server = build_server(serve_dir, bound_port, current_project=target.slug)
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
        if not kuzu_released:
            _close_kuzu_connection()
