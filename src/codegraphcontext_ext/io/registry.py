"""Command registry data for kkg manifest.

Lives in ``io/`` (not ``commands/``) so tests and agent tooling can
import the registry without triggering the full commands-package init
and its transitive dependencies (pathspec, tree-sitter, etc.).
"""

from __future__ import annotations

from typing import Any

# ── Command registry ────────────────────────────────────────────────
#
# Each entry describes one kkg extension command.  Fields:
#   name                CLI name (as registered in cli.py)
#   summary             One-line human description
#   schema              Filename in schemas/ (null if no schema)
#   project_aware       Accepts --project and calls activate_project
#   touches_graph_store Opens a connection to the active graph store
#                       (FalkorDB Lite by default; KuzuDB / Neo4j when
#                       the user selects them explicitly).
#   output_modes        List of output formats the command can produce
#   server              True if the command starts a long-running process
#   prereqs             Env vars or services required at runtime. The
#                       canonical backend-path var is listed for the
#                       default backend (FALKORDB_PATH); commands
#                       invoked with ``--database kuzudb`` expect
#                       ``KUZUDB_PATH`` instead.

COMMAND_REGISTRY: list[dict[str, Any]] = [
    {
        "name": "advise",
        "summary": "Advisory tip lookup: situational suggestions for btrain workflows.",
        "schema": "advise.json",
        "project_aware": False,
        "touches_graph_store": False,
        "output_modes": ["json"],
        "server": False,
        "prereqs": [],
    },
    {
        "name": "audit",
        "summary": "Run code-quality standards against the graph and report violations.",
        "schema": "audit.json",
        "project_aware": True,
        "touches_graph_store": True,
        "output_modes": ["json", "summary"],
        "server": False,
        "prereqs": ["FALKORDB_PATH"],
    },
    {
        "name": "blast-radius",
        "summary": "Pre-lock collision check: expand file set through the graph and detect lane overlaps.",
        "schema": "blast-radius.json",
        "project_aware": True,
        "touches_graph_store": True,
        "output_modes": ["json"],
        "server": False,
        "prereqs": ["FALKORDB_PATH"],
    },
    {
        "name": "clusters",
        "summary": "Surface Louvain community detection results from the code graph.",
        "schema": "clusters.json",
        "project_aware": True,
        "touches_graph_store": True,
        "output_modes": ["json"],
        "server": False,
        "prereqs": ["FALKORDB_PATH"],
    },
    {
        "name": "doctor",
        "summary": "Validate setup: backend, DB access, graph, embeddings, PATH.",
        "schema": None,
        "project_aware": True,
        "touches_graph_store": True,
        "output_modes": ["json"],
        "server": False,
        "prereqs": ["FALKORDB_PATH"],
    },
    {
        "name": "drift-check",
        "summary": "Detect graph-neighborhood changes outside a lane's locked files.",
        "schema": "drift-check.json",
        "project_aware": True,
        "touches_graph_store": True,
        "output_modes": ["json"],
        "server": False,
        "prereqs": ["FALKORDB_PATH"],
    },
    {
        "name": "health",
        "summary": "A-F letter-grade health score computed from audit violations.",
        "schema": "health.json",
        "project_aware": True,
        "touches_graph_store": True,
        "output_modes": ["json"],
        "server": False,
        "prereqs": ["FALKORDB_PATH"],
    },
    {
        "name": "hotspots",
        "summary": "Identify high-risk code via git churn x graph centrality analysis.",
        "schema": "hotspots.json",
        "project_aware": True,
        "touches_graph_store": True,
        "output_modes": ["json"],
        "server": False,
        "prereqs": ["FALKORDB_PATH"],
    },
    {
        "name": "entrypoints",
        "summary": "Score and rank code entities as entry points by decorators and in-degree.",
        "schema": "entrypoints.json",
        "project_aware": True,
        "touches_graph_store": True,
        "output_modes": ["json"],
        "server": False,
        "prereqs": ["FALKORDB_PATH"],
    },
    {
        "name": "execution-flow",
        "summary": "Trace the call chain from a symbol through the code graph.",
        "schema": "execution-flow.json",
        "project_aware": True,
        "touches_graph_store": True,
        "output_modes": ["json"],
        "server": False,
        "prereqs": ["FALKORDB_PATH"],
    },
    {
        "name": "embed",
        "summary": "Vectorize code-entity nodes in the graph store for hybrid retrieval.",
        "schema": None,
        "project_aware": True,
        "touches_graph_store": True,
        "output_modes": ["json"],
        "server": False,
        "prereqs": ["FALKORDB_PATH", "HF_HOME"],
    },
    {
        "name": "impact",
        "summary": "Symbol-oriented impact analysis: expand a function or class through the call graph.",
        "schema": "impact.json",
        "project_aware": True,
        "touches_graph_store": True,
        "output_modes": ["json"],
        "server": False,
        "prereqs": ["FALKORDB_PATH"],
    },
    {
        "name": "repl",
        "summary": "Interactive session with sticky project, profile, and query history.",
        "schema": None,
        "project_aware": True,
        "touches_graph_store": True,
        "output_modes": ["json"],
        "server": False,
        "prereqs": ["FALKORDB_PATH", "HF_HOME"],
    },
    {
        "name": "export-embeddings",
        "summary": "Export embedding vectors as TSVs for TF Embedding Projector.",
        "schema": None,
        "project_aware": True,
        "touches_graph_store": True,
        "output_modes": ["json"],
        "server": False,
        "prereqs": ["FALKORDB_PATH"],
    },
    {
        "name": "manifest",
        "summary": "Emit a machine-readable manifest of all kkg commands.",
        "schema": "manifest.json",
        "project_aware": False,
        "touches_graph_store": False,
        "output_modes": ["json"],
        "server": False,
        "prereqs": [],
    },
    {
        "name": "review-packet",
        "summary": "Generate a reviewer JSON packet with blast radius and advisories.",
        "schema": "review-packet.json",
        "project_aware": True,
        "touches_graph_store": True,
        "output_modes": ["json"],
        "server": False,
        "prereqs": ["FALKORDB_PATH"],
    },
    {
        "name": "snapshot",
        "summary": "Capture a point-in-time snapshot of graph metrics for trend tracking.",
        "schema": "snapshot.json",
        "project_aware": True,
        "touches_graph_store": True,
        "output_modes": ["json"],
        "server": False,
        "prereqs": ["FALKORDB_PATH"],
    },
    {
        "name": "search",
        "summary": "Semantic search: ANN vector search + graph neighborhood expansion.",
        "schema": "context.json",
        "project_aware": True,
        "touches_graph_store": True,
        "output_modes": ["json"],
        "server": False,
        "prereqs": ["FALKORDB_PATH", "HF_HOME"],
    },
    {
        "name": "serve",
        "summary": "Start warm daemon on a Unix socket to eliminate cold-start latency.",
        "schema": None,
        "project_aware": False,
        "touches_graph_store": True,
        "output_modes": ["json"],
        "server": True,
        "prereqs": ["FALKORDB_PATH"],
    },
    {
        "name": "serve-localhost",
        "summary": "Start warm daemon on localhost TCP and retry nearby ports until one binds.",
        "schema": None,
        "project_aware": False,
        "touches_graph_store": True,
        "output_modes": ["json"],
        "server": True,
        "prereqs": ["FALKORDB_PATH"],
    },
    {
        "name": "sync-check",
        "summary": "Report upstream commits not yet merged into the cgraph fork.",
        "schema": "sync-check.json",
        "project_aware": False,
        "touches_graph_store": False,
        "output_modes": ["json"],
        "server": False,
        "prereqs": [],
    },
    {
        "name": "viz-dashboard",
        "summary": "Unified dashboard: 2D graph, 3D graph, embeddings scatter, and TF Projector as tabs.",
        "schema": None,
        "project_aware": True,
        "touches_graph_store": True,
        "output_modes": ["json", "html"],
        "server": False,
        "prereqs": ["FALKORDB_PATH"],
    },
    {
        "name": "viz-embeddings",
        "summary": "Interactive 2D scatter plot of code embedding vectors.",
        "schema": None,
        "project_aware": True,
        "touches_graph_store": True,
        "output_modes": ["json", "html"],
        "server": False,
        "prereqs": ["FALKORDB_PATH"],
    },
    {
        "name": "viz-graph",
        "summary": "Interactive Cytoscape.js graph of code structure.",
        "schema": None,
        "project_aware": True,
        "touches_graph_store": True,
        "output_modes": ["json", "html"],
        "server": False,
        "prereqs": ["FALKORDB_PATH"],
    },
    {
        "name": "viz-projector",
        "summary": "Serve the TF Embedding Projector locally with cgraph embeddings pre-loaded.",
        "schema": None,
        "project_aware": True,
        "touches_graph_store": True,
        "output_modes": ["json", "html"],
        "server": False,
        "prereqs": ["FALKORDB_PATH"],
    },
]


def get_command_registry() -> list[dict[str, Any]]:
    """Return a copy of the full command registry."""
    return list(COMMAND_REGISTRY)
