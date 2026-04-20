"""kkg snapshot: point-in-time graph metrics capture.

Phase 5 — Captures node/edge counts, embedding coverage, git HEAD,
and audit summary into a timestamped JSON snapshot.  Useful for trend
tracking, CI dashboards, and pre/post-refactor comparisons.

Usage:
    kkg snapshot
    kkg snapshot --project flask
    kkg snapshot --save          # write to .cgraph/snapshots/
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Optional

import typer

from ..config import resolve_cgraph_config
from ..io.json_stdout import emit_json, make_envelope
from ..io.kuzu import get_kuzu_connection
from ..project import PROJECT_OPTION_HELP, _normalize_slug, activate_project

COMMAND_NAME = "snapshot"
SCHEMA_FILE = "snapshot.json"
SUMMARY = "Capture a point-in-time snapshot of graph metrics for trend tracking."

# Node types to count
_NODE_TYPES = [
    "Repository", "File", "Directory", "Module",
    "Function", "Class", "Variable", "Trait",
    "Interface", "Macro", "Struct", "Enum",
    "Union", "Annotation", "Record", "Property", "Parameter",
]

# Edge types to count
_EDGE_TYPES = [
    "CALLS", "IMPORTS", "INHERITS", "CONTAINS",
    "HAS_PARAMETER", "INCLUDES", "IMPLEMENTS",
]


def _query_node_counts(conn) -> dict:
    """Count nodes per type."""
    counts = {}
    for ntype in _NODE_TYPES:
        try:
            result = conn.execute(f"MATCH (n:{ntype}) RETURN count(n) AS c")
            row = result.get_next()
            counts[ntype] = row[0] if row else 0
        except Exception:
            counts[ntype] = 0
    return counts


def _query_edge_counts(conn) -> dict:
    """Count edges per type."""
    counts = {}
    for etype in _EDGE_TYPES:
        try:
            result = conn.execute(f"MATCH ()-[r:{etype}]->() RETURN count(r) AS c")
            row = result.get_next()
            counts[etype] = row[0] if row else 0
        except Exception:
            counts[etype] = 0
    return counts


def _query_embedding_coverage(conn) -> dict:
    """Check how many Function/Class nodes have embeddings."""
    coverage = {}
    for table in ("Function", "Class"):
        try:
            total_result = conn.execute(f"MATCH (n:{table}) RETURN count(n) AS c")
            total_row = total_result.get_next()
            total = total_row[0] if total_row else 0

            embedded_result = conn.execute(
                f"MATCH (n:{table}) WHERE n.embedding IS NOT NULL "
                f"RETURN count(n) AS c"
            )
            embedded_row = embedded_result.get_next()
            embedded = embedded_row[0] if embedded_row else 0

            coverage[table] = {
                "total": total,
                "embedded": embedded,
                "coverage_pct": round(embedded / total * 100, 1) if total > 0 else 0.0,
            }
        except Exception:
            coverage[table] = {"total": 0, "embedded": 0, "coverage_pct": 0.0}
    return coverage


def _get_git_head(cwd: Optional[str] = None) -> dict:
    """Get git HEAD info, optionally from a specific directory.

    When *cwd* is given, git runs in that directory so the snapshot
    reports the target project's HEAD rather than the tool repo's.
    When *cwd* is None, uses the current working directory (correct
    when the tool repo IS the target project).
    """
    kwargs: dict = {"stderr": subprocess.DEVNULL, "text": True}
    if cwd is not None:
        kwargs["cwd"] = cwd
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], **kwargs,
        ).strip()
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], **kwargs,
        ).strip()
        return {"sha": sha, "branch": branch}
    except Exception:
        return {"sha": None, "branch": None}


# ---------------------------------------------------------------------------
# Payload builder (pure-ish, testable)
# ---------------------------------------------------------------------------

def build_snapshot_payload(
    *,
    project: Optional[str] = None,
    conn,
    source_checkout: Optional[str] = None,
) -> dict:
    """Build the snapshot payload from graph metrics.

    *conn* is a live KùzuDB connection obtained by the caller (typically
    via ``get_kuzu_connection()`` in the CLI path, which runs the Phase
    1.5 storage preflight).

    *source_checkout* is the path to the target project's git checkout.
    When set, git metadata comes from that directory.  When a *project*
    is given but *source_checkout* is None, git metadata is returned as
    null to avoid silently reporting the tool repo's HEAD.
    """
    nodes = _query_node_counts(conn)
    edges = _query_edge_counts(conn)
    embeddings = _query_embedding_coverage(conn)

    # Only read git metadata from an explicit source checkout.
    # When targeting a remote project with no local checkout, return
    # null rather than accidentally reporting the tool repo's HEAD.
    if project is not None and source_checkout is None:
        git = {"sha": None, "branch": None}
    else:
        git = _get_git_head(cwd=source_checkout)

    total_nodes = sum(nodes.values())
    total_edges = sum(edges.values())

    total_embedded = sum(e["embedded"] for e in embeddings.values())
    total_embeddable = sum(e["total"] for e in embeddings.values())

    return make_envelope("snapshot", {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "git": git,
        "nodes": nodes,
        "edges": edges,
        "embeddings": embeddings,
        "totals": {
            "nodes": total_nodes,
            "edges": total_edges,
            "embedded": total_embedded,
            "embeddable": total_embeddable,
            "embedding_coverage_pct": (
                round(total_embedded / total_embeddable * 100, 1)
                if total_embeddable > 0 else 0.0
            ),
        },
    }, project=project)


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------

def snapshot_command(
    project: Optional[str] = typer.Option(
        None,
        "--project",
        help=PROJECT_OPTION_HELP,
    ),
) -> None:
    """Capture a point-in-time snapshot of graph metrics."""
    target = activate_project(project)

    # get_kuzu_connection() runs require_storage() internally.
    # If storage is offline, it prints the storage_offline JSON and
    # raises SystemExit — preserving the Phase 1.5 contract.
    conn = get_kuzu_connection()

    # Resolve source checkout so git metadata comes from the target
    # project, not the tool repo's cwd.  The config's source_checkout
    # is specific to the local project; only use it when the target
    # matches (i.e. was resolved from config/basename, not an explicit
    # --project pointing at a different repo).
    cfg = resolve_cgraph_config()
    source_checkout = None
    if cfg.source_checkout and cfg.source_checkout.is_dir():
        if target.source != "cli":
            # Local project — config's source_checkout is valid.
            source_checkout = str(cfg.source_checkout)
        else:
            # Explicit --project: check if it matches the config's
            # implied project by normalizing the checkout dir name
            # through the same slug logic used by resolve_project_target.
            try:
                checkout_slug = _normalize_slug(cfg.source_checkout.name)
            except ValueError:
                checkout_slug = ""
            if checkout_slug == target.slug:
                source_checkout = str(cfg.source_checkout)

    payload = build_snapshot_payload(
        project=target.slug, conn=conn, source_checkout=source_checkout,
    )

    # Human-readable summary to stderr
    totals = payload.get("totals", {})
    print(
        f"Snapshot: {totals.get('nodes', 0)} nodes, "
        f"{totals.get('edges', 0)} edges, "
        f"{totals.get('embedding_coverage_pct', 0)}% embedded",
        file=sys.stderr,
    )

    typer.echo(emit_json(payload))
    raise typer.Exit(code=0 if payload.get("ok") else 1)
