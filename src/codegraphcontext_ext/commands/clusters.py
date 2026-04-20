"""kkg clusters: surface Louvain communities from the code graph.

Phase 5.7 turns the existing topology community detection into a
machine-readable CLI command with the standard cgraph JSON envelope.
"""

from __future__ import annotations

import sys
from typing import Any, Optional

import typer

from ..io.json_stdout import emit_json, make_envelope
from ..io.kuzu import get_kuzu_connection
from ..project import PROJECT_OPTION_HELP, activate_project
from ..topology.communities import fetch_community_data

COMMAND_NAME = "clusters"
SCHEMA_FILE = "clusters.json"
SUMMARY = "Surface Louvain community detection results from the code graph."

_DEFAULT_SEMANTIC_THRESHOLD = 0.85
_DEFAULT_MAX_SEMANTIC_NODES = 2000


def _empty_community_data() -> dict[str, Any]:
    return {
        "communities": [],
        "edges": [],
        "cross_edges": [],
        "stats": {
            "total_nodes": 0,
            "total_edges": 0,
            "communities": 0,
            "structural_edges": 0,
            "semantic_edges": 0,
            "cross_community_edges": 0,
        },
    }


def build_clusters_payload(
    *,
    conn: Any | None,
    semantic_threshold: float = _DEFAULT_SEMANTIC_THRESHOLD,
    max_semantic_nodes: int = _DEFAULT_MAX_SEMANTIC_NODES,
    project: str | None = None,
) -> dict[str, Any]:
    """Build the JSON payload for ``kkg clusters``."""
    payload_data = _empty_community_data()
    payload_data["parameters"] = {
        "semantic_threshold": semantic_threshold,
        "max_semantic_nodes": max_semantic_nodes,
    }
    payload_data["advisories"] = []

    if conn is None:
        payload_data["advisories"].append({
            "level": "warn",
            "kind": "no_graph",
            "detail": "Could not connect to KuzuDB; community detection skipped.",
        })
        return make_envelope(COMMAND_NAME, payload_data, project=project)

    try:
        payload_data.update(
            fetch_community_data(
                conn,
                semantic_threshold=semantic_threshold,
                max_semantic_nodes=max_semantic_nodes,
            )
        )
    except Exception as exc:
        return make_envelope(
            COMMAND_NAME,
            payload_data,
            ok=False,
            error=f"Community detection failed: {exc}",
            project=project,
        )

    return make_envelope(COMMAND_NAME, payload_data, project=project)


def clusters_command(
    semantic_threshold: float = typer.Option(
        _DEFAULT_SEMANTIC_THRESHOLD,
        "--semantic-threshold",
        min=0.0,
        max=1.0,
        help="Cosine threshold for inferred SEMANTICALLY_SIMILAR edges.",
    ),
    max_semantic_nodes: int = typer.Option(
        _DEFAULT_MAX_SEMANTIC_NODES,
        "--max-semantic-nodes",
        min=0,
        help="Cap Function nodes fetched for semantic edge generation.",
    ),
    project: Optional[str] = typer.Option(
        None,
        "--project",
        help=PROJECT_OPTION_HELP,
    ),
) -> None:
    """Run Louvain community detection over the current project graph."""
    target = activate_project(project)

    conn = None
    try:
        conn = get_kuzu_connection()
    except Exception as exc:
        print(
            f"Warning: KuzuDB unavailable ({exc}); community detection skipped.",
            file=sys.stderr,
        )

    payload = build_clusters_payload(
        conn=conn,
        semantic_threshold=semantic_threshold,
        max_semantic_nodes=max_semantic_nodes,
        project=target.slug,
    )

    typer.echo(emit_json(payload))
    raise typer.Exit(code=0 if payload.get("ok") else 1)
