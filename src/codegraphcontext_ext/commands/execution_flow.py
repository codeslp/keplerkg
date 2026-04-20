"""kkg execution-flow: summarized call chain from a symbol.

Phase 5.7: traces CALLS edges forward from a function or class,
returning an ordered execution tree that shows what a symbol calls
and what those callees call in turn.  Useful for understanding
control flow before making changes.

Usage:
    kkg execution-flow --symbol "handle_request"
    kkg execution-flow --symbol "UserService.login" --depth 5
"""

from __future__ import annotations

import sys
from collections import deque
from typing import Any, Optional

import typer

from ..io.json_stdout import emit_json, make_envelope
from ..io.kuzu import get_kuzu_connection
from ..project import PROJECT_OPTION_HELP, activate_project

COMMAND_NAME = "execution-flow"
SCHEMA_FILE = "execution-flow.json"
SUMMARY = "Trace the call chain from a symbol through the code graph."

_CODE_NODE_TABLES = (
    "Function", "Class", "Variable", "Trait", "Interface",
    "Macro", "Struct", "Enum", "Union", "Annotation", "Record", "Property",
)

_DEFAULT_DEPTH = 4
_DEFAULT_MAX_NODES = 100


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------

def _find_symbols(
    conn: Any,
    symbol: str,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    """Find graph nodes matching a symbol name."""
    if conn is None:
        return []

    tables = (kind,) if kind and kind in _CODE_NODE_TABLES else _CODE_NODE_TABLES
    matches: list[dict[str, Any]] = []
    seen: set[str] = set()

    for table in tables:
        query = (
            f"MATCH (n:`{table}`) "
            f"WHERE n.name = $name "
            f"RETURN n.uid AS uid, n.name AS name, n.path AS path, "
            f"n.line_number AS line_number, label(n) AS kind"
        )
        try:
            result = conn.execute(query, parameters={"name": symbol})
            while result.has_next():
                row = result.get_next()
                uid = row[0]
                if uid and uid not in seen:
                    seen.add(uid)
                    matches.append({
                        "uid": uid,
                        "name": row[1],
                        "file": f"{row[2]}:{row[3]}" if row[2] and row[3] else row[2],
                        "kind": row[4],
                    })
        except Exception:
            continue

    return matches


def _single_hop_callees(
    conn: Any,
    source_uids: set[str],
    visited: set[str],
) -> list[dict[str, Any]]:
    """One-hop forward: find direct callees of source_uids not yet visited."""
    if not source_uids or conn is None:
        return []

    uid_list = ", ".join(f"'{u}'" for u in source_uids)
    visited_list = ", ".join(f"'{u}'" for u in visited) if visited else "''"

    query = (
        f"MATCH (source)-[r:CALLS]->(callee) "
        f"WHERE source.uid IN [{uid_list}] AND NOT callee.uid IN [{visited_list}] "
        f"RETURN DISTINCT source.uid AS caller_uid, callee.uid AS uid, "
        f"callee.name AS name, callee.path AS path, "
        f"callee.line_number AS line_number, label(callee) AS kind"
    )

    results: list[dict[str, Any]] = []
    try:
        result = conn.execute(query)
        while result.has_next():
            row = result.get_next()
            uid = row[1]
            if uid and uid not in visited:
                results.append({
                    "caller_uid": row[0],
                    "uid": uid,
                    "name": row[2],
                    "file": f"{row[3]}:{row[4]}" if row[3] and row[4] else row[3],
                    "kind": row[5],
                })
    except Exception:
        pass

    return results


def _build_call_tree(
    conn: Any,
    roots: list[dict[str, Any]],
    *,
    depth: int,
    max_nodes: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """BFS forward through CALLS edges, building a flat edge list + node list.

    Returns (nodes, edges) where:
      nodes: [{uid, name, file, kind, depth}, ...]
      edges: [{source, target}, ...]
    """
    if conn is None or not roots:
        return [], []

    all_nodes: dict[str, dict[str, Any]] = {}
    all_edges: list[dict[str, str]] = []
    visited: set[str] = set()

    # Seed the roots
    for root in roots:
        uid = root["uid"]
        visited.add(uid)
        all_nodes[uid] = {
            "uid": uid,
            "name": root["name"],
            "file": root.get("file"),
            "kind": root.get("kind"),
            "depth": 0,
        }

    frontier = {r["uid"] for r in roots}

    for current_depth in range(depth):
        if not frontier or len(all_nodes) >= max_nodes:
            break

        callees = _single_hop_callees(conn, frontier, visited)
        next_frontier: set[str] = set()

        for callee in callees:
            uid = callee["uid"]
            all_edges.append({
                "source": callee["caller_uid"],
                "target": uid,
            })
            if uid not in visited:
                visited.add(uid)
                next_frontier.add(uid)
                all_nodes[uid] = {
                    "uid": uid,
                    "name": callee["name"],
                    "file": callee["file"],
                    "kind": callee["kind"],
                    "depth": current_depth + 1,
                }

            if len(all_nodes) >= max_nodes:
                break

        frontier = next_frontier

    return list(all_nodes.values()), all_edges


# ---------------------------------------------------------------------------
# Payload builder (pure, testable)
# ---------------------------------------------------------------------------

def build_execution_flow_payload(
    *,
    symbol: str,
    kind: str | None = None,
    depth: int = _DEFAULT_DEPTH,
    max_nodes: int = _DEFAULT_MAX_NODES,
    conn: Any = None,
    project: Optional[str] = None,
) -> dict[str, Any]:
    """Build the execution-flow response payload.

    Args:
        symbol: name of the root function/class.
        kind: optional node-type filter.
        depth: max forward hops.
        max_nodes: total node cap across all depths.
        conn: KùzuDB connection (may be None).
        project: resolved project slug for the envelope.
    """
    advisories: list[dict[str, str]] = []

    roots = _find_symbols(conn, symbol, kind)

    if not roots and conn is not None:
        advisories.append({
            "level": "warn",
            "kind": "symbol_not_found",
            "detail": f"No graph node named '{symbol}'"
            + (f" of kind {kind}" if kind else "")
            + ". Run kkg embed first?",
        })

    nodes, edges = _build_call_tree(conn, roots, depth=depth, max_nodes=max_nodes)

    truncated = len(nodes) >= max_nodes
    if truncated:
        advisories.append({
            "level": "info",
            "kind": "truncated",
            "detail": f"Flow capped at {max_nodes} nodes.",
        })

    if conn is None:
        advisories.append({
            "level": "warn",
            "kind": "no_graph",
            "detail": "KùzuDB unavailable; execution flow empty.",
        })

    # Compute depth distribution
    depth_counts: dict[int, int] = {}
    for node in nodes:
        d = node.get("depth", 0)
        depth_counts[d] = depth_counts.get(d, 0) + 1

    return make_envelope("execution_flow", {
        "symbol": symbol,
        "kind_filter": kind,
        "roots": roots,
        "nodes": nodes,
        "edges": edges,
        "advisories": advisories,
        "summary": {
            "roots": len(roots),
            "total_nodes": len(nodes),
            "total_edges": len(edges),
            "max_depth_reached": max(
                (n.get("depth", 0) for n in nodes), default=0
            ),
            "depth_distribution": depth_counts,
        },
        "truncated": truncated,
    }, project=project)


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------

def execution_flow_command(
    symbol: str = typer.Option(
        ...,
        "--symbol",
        help="Function, class, or entity name to trace.",
    ),
    kind: Optional[str] = typer.Option(
        None,
        "--kind",
        help="Node type filter: Function, Class, etc.",
    ),
    depth: int = typer.Option(
        _DEFAULT_DEPTH,
        "--depth",
        min=1,
        help="Max forward hops through CALLS edges (default 4).",
    ),
    max_nodes: int = typer.Option(
        _DEFAULT_MAX_NODES,
        "--max-nodes",
        min=1,
        help="Total node cap across all depths (default 100).",
    ),
    project: Optional[str] = typer.Option(
        None,
        "--project",
        help=PROJECT_OPTION_HELP,
    ),
) -> None:
    """Trace the call chain from a symbol through the code graph.

    Walks CALLS edges forward from the named function or class, building
    an execution-flow tree that shows what the symbol calls and what
    those callees call in turn.
    """
    target = activate_project(project)

    conn = None
    try:
        conn = get_kuzu_connection()
    except Exception as exc:
        print(
            f"Warning: KùzuDB unavailable ({exc}); flow will be empty.",
            file=sys.stderr,
        )

    payload = build_execution_flow_payload(
        symbol=symbol,
        kind=kind,
        depth=depth,
        max_nodes=max_nodes,
        conn=conn,
        project=target.slug,
    )

    typer.echo(emit_json(payload))
    raise typer.Exit(code=0 if payload.get("ok") else 1)
