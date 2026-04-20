"""kkg impact: symbol-oriented blast-radius for agent consumption.

Phase 5.7: wraps the graph-expansion logic from blast_radius.py but
takes a *symbol name* instead of file paths, making it natural for
agents that already know the function/class they care about.

Usage:
    kkg impact --symbol "authenticate_user"
    kkg impact --symbol "UserService" --kind Class --depth 3
"""

from __future__ import annotations

import sys
from typing import Any, Optional

import typer

from ..framework.resolver import classify_decorators
from ..io.json_stdout import emit_json, make_envelope
from ..io.kuzu import get_kuzu_connection
from ..project import PROJECT_OPTION_HELP, activate_project

COMMAND_NAME = "impact"
SCHEMA_FILE = "impact.json"
SUMMARY = "Symbol-oriented impact analysis: expand a function or class through the call graph."

# Node tables for symbol lookup — code entities only.
_CODE_NODE_TABLES = (
    "Function", "Class", "Variable", "Trait", "Interface",
    "Macro", "Struct", "Enum", "Union", "Annotation", "Record", "Property",
)

_DEFAULT_DEPTH = 3
_DEFAULT_MAX_NODES = 50


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------

def _find_symbols(
    conn: Any,
    symbol: str,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    """Find graph nodes matching a symbol name, optionally filtered by kind."""
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


def _bfs_expand(
    conn: Any,
    seed_uids: set[str],
    *,
    direction: str,
    depth: int,
    max_nodes: int,
) -> list[dict[str, Any]]:
    """BFS expansion through CALLS edges in the given direction.

    direction: "callers" (incoming) or "callees" (outgoing).
    """
    if not seed_uids or conn is None:
        return []

    all_found: dict[str, dict[str, Any]] = {}
    frontier = set(seed_uids)
    visited = set(seed_uids)

    for hop in range(depth):
        if not frontier:
            break
        uid_list = ", ".join(f"'{u}'" for u in frontier)

        if direction == "callers":
            query = (
                f"MATCH (caller)-[r:CALLS]->(target) "
                f"WHERE target.uid IN [{uid_list}] AND NOT caller.uid IN [{', '.join(repr(u) for u in visited)}] "
                f"RETURN DISTINCT caller.uid AS uid, caller.name AS name, "
                f"caller.path AS path, caller.line_number AS line_number, "
                f"label(caller) AS kind"
            )
        else:
            query = (
                f"MATCH (source)-[r:CALLS]->(callee) "
                f"WHERE source.uid IN [{uid_list}] AND NOT callee.uid IN [{', '.join(repr(u) for u in visited)}] "
                f"RETURN DISTINCT callee.uid AS uid, callee.name AS name, "
                f"callee.path AS path, callee.line_number AS line_number, "
                f"label(callee) AS kind"
            )

        next_frontier: set[str] = set()
        try:
            result = conn.execute(query)
            while result.has_next():
                row = result.get_next()
                uid = row[0]
                if uid and uid not in visited:
                    visited.add(uid)
                    next_frontier.add(uid)
                    all_found[uid] = {
                        "uid": uid,
                        "name": row[1],
                        "file": f"{row[2]}:{row[3]}" if row[2] and row[3] else row[2],
                        "kind": row[4],
                        "hops": hop + 1,
                    }
        except Exception:
            break

        frontier = next_frontier

        if len(all_found) >= max_nodes:
            break

    results = list(all_found.values())
    if len(results) > max_nodes:
        results = results[:max_nodes]
    return results


def _annotate_entrypoints(
    conn: Any,
    nodes: list[dict[str, Any]],
) -> int:
    """Annotate nodes with entry-point framework info in-place.

    Queries decorator metadata for Function nodes and adds ``framework``
    and ``entry_category`` fields when a known framework is detected.
    Returns the count of entry-point nodes found.
    """
    if not nodes or conn is None:
        return 0

    func_uids = [n["uid"] for n in nodes if n.get("kind") == "Function"]
    if not func_uids:
        return 0

    uid_list = ", ".join(f"'{u}'" for u in func_uids)
    decorators_by_uid: dict[str, list[str]] = {}

    query = (
        f"MATCH (f:Function) "
        f"WHERE f.uid IN [{uid_list}] AND f.decorators IS NOT NULL "
        f"RETURN f.uid, f.decorators"
    )
    try:
        result = conn.execute(query)
        while result.has_next():
            row = result.get_next()
            if row[0] and row[1]:
                decorators_by_uid[row[0]] = list(row[1])
    except Exception:
        return 0

    count = 0
    for node in nodes:
        decorators = decorators_by_uid.get(node["uid"])
        if not decorators:
            continue
        match = classify_decorators(decorators)
        if match:
            node["framework"] = match.framework
            node["entry_category"] = match.category
            count += 1

    return count


def _find_cross_module_imports(
    conn: Any,
    file_paths: list[str],
) -> list[str]:
    """Find top-level modules imported by files containing the matched symbols."""
    if not file_paths or conn is None:
        return []

    path_list = ", ".join(f"'{p}'" for p in file_paths)
    modules: set[str] = set()

    query = (
        f"MATCH (f:File)-[r:IMPORTS]->(m:Module) "
        f"WHERE f.path IN [{path_list}] "
        f"RETURN DISTINCT m.name AS module_name"
    )
    try:
        result = conn.execute(query)
        while result.has_next():
            row = result.get_next()
            if row[0]:
                modules.add(row[0].split(".")[0])
    except Exception:
        pass

    return sorted(modules)


# ---------------------------------------------------------------------------
# Payload builder (pure, testable)
# ---------------------------------------------------------------------------

def build_impact_payload(
    *,
    symbol: str,
    kind: str | None = None,
    depth: int = _DEFAULT_DEPTH,
    max_nodes: int = _DEFAULT_MAX_NODES,
    conn: Any = None,
    project: Optional[str] = None,
) -> dict[str, Any]:
    """Build the impact analysis payload.

    Args:
        symbol: name of the function/class/entity to analyze.
        kind: optional node-type filter (e.g. "Function", "Class").
        depth: max BFS hops for caller/callee expansion.
        max_nodes: per-bucket cap.
        conn: KùzuDB connection (may be None).
        project: resolved project slug for the envelope.
    """
    advisories: list[dict[str, str]] = []

    # Resolve symbol
    matches = _find_symbols(conn, symbol, kind)

    if not matches and conn is not None:
        advisories.append({
            "level": "warn",
            "kind": "symbol_not_found",
            "detail": f"No graph node named '{symbol}'"
            + (f" of kind {kind}" if kind else "")
            + ". Run kkg embed first?",
        })

    seed_uids = {m["uid"] for m in matches}

    # Expand callers and callees
    callers = _bfs_expand(conn, seed_uids, direction="callers", depth=depth, max_nodes=max_nodes)
    callees = _bfs_expand(conn, seed_uids, direction="callees", depth=depth, max_nodes=max_nodes)

    # Annotate entry points in callers/callees
    ep_callers = _annotate_entrypoints(conn, callers)
    ep_callees = _annotate_entrypoints(conn, callees)
    _annotate_entrypoints(conn, matches)

    # Cross-module impact from the files containing the matched symbols
    symbol_files = list({
        m["file"].split(":")[0] for m in matches if m.get("file") and ":" in m["file"]
    })
    cross_module = _find_cross_module_imports(conn, symbol_files)

    # Truncation
    truncated = len(callers) >= max_nodes or len(callees) >= max_nodes
    if truncated:
        advisories.append({
            "level": "info",
            "kind": "truncated",
            "detail": f"Results capped at {max_nodes} per direction.",
        })

    if conn is None:
        advisories.append({
            "level": "warn",
            "kind": "no_graph",
            "detail": "KùzuDB unavailable; impact computed without graph data.",
        })

    return make_envelope("impact", {
        "symbol": symbol,
        "kind_filter": kind,
        "matches": matches,
        "callers": callers,
        "callees": callees,
        "cross_module_impact": cross_module,
        "advisories": advisories,
        "summary": {
            "matches": len(matches),
            "callers": len(callers),
            "callees": len(callees),
            "cross_modules": len(cross_module),
            "depth": depth,
            "entrypoint_callers": ep_callers,
            "entrypoint_callees": ep_callees,
        },
        "truncated": truncated,
    }, project=project)


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------

def impact_command(
    symbol: str = typer.Option(
        ...,
        "--symbol",
        help="Function, class, or entity name to analyze.",
    ),
    kind: Optional[str] = typer.Option(
        None,
        "--kind",
        help="Node type filter: Function, Class, Variable, etc.",
    ),
    depth: int = typer.Option(
        _DEFAULT_DEPTH,
        "--depth",
        min=1,
        help="Max BFS hops for caller/callee expansion (default 3).",
    ),
    max_nodes: int = typer.Option(
        _DEFAULT_MAX_NODES,
        "--max-nodes",
        min=1,
        help="Per-direction node cap (default 50).",
    ),
    project: Optional[str] = typer.Option(
        None,
        "--project",
        help=PROJECT_OPTION_HELP,
    ),
) -> None:
    """Symbol-oriented impact analysis.

    Resolves a function or class name in the code graph, then expands
    callers and callees to show the blast radius of changes to that symbol.
    """
    target = activate_project(project)

    conn = None
    try:
        conn = get_kuzu_connection()
    except Exception as exc:
        print(
            f"Warning: KùzuDB unavailable ({exc}); graph data will be empty.",
            file=sys.stderr,
        )

    payload = build_impact_payload(
        symbol=symbol,
        kind=kind,
        depth=depth,
        max_nodes=max_nodes,
        conn=conn,
        project=target.slug,
    )

    typer.echo(emit_json(payload))
    raise typer.Exit(code=0 if payload.get("ok") else 1)
