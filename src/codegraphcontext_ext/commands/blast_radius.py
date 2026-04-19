"""kkg blast-radius: pre-lock collision check via graph expansion.

Spec §4: expands a requested lock-file set through the graph and reports
transitive callers/callees outside the requested paths, plus overlap with
other active btrain lanes.  Also the primary producer of `lock_overlap`
advisories consumed by `kkg advise` (§5.1).

Timeout budget: 2s (adapter-side; this command does not self-enforce).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

import typer

from ..io.json_stdout import emit_json
from ..io.kuzu import get_kuzu_connection
from ..project import PROJECT_OPTION_HELP, activate_project

COMMAND_NAME = "blast-radius"
SCHEMA_FILE = "blast-radius.json"
SUMMARY = "Pre-lock collision check: expand file set through the graph and detect lane overlaps."

# Node tables to query — same set as review-packet.
_CODE_NODE_TABLES = (
    "Function", "Class", "Variable", "Trait", "Interface",
    "Macro", "Struct", "Enum", "Union", "Annotation", "Record", "Property",
)

_DEFAULT_MAX_NODES = 50


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _repo_root(cwd: Path | None = None) -> Path:
    return (cwd or Path.cwd()).resolve()


def _path_variants(path: str, cwd: Path | None = None) -> set[str]:
    """Return both relative and absolute forms of a path for matching."""
    variants = {Path(path).as_posix()}
    root = _repo_root(cwd)
    abs_path = (root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    variants.add(str(abs_path))
    return {v for v in variants if v}


def _query_path_list(file_paths: list[str], cwd: Path | None = None) -> list[str]:
    query_paths: set[str] = set()
    for path in file_paths:
        query_paths.update(_path_variants(path, cwd))
    return sorted(query_paths)


def _display_path(path: str | None, cwd: Path | None = None) -> str | None:
    if not path:
        return path
    p = Path(path)
    if not p.is_absolute():
        return p.as_posix()
    try:
        return p.resolve().relative_to(_repo_root(cwd)).as_posix()
    except ValueError:
        return p.as_posix()


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------

def _find_nodes_by_paths(
    conn: Any, file_paths: list[str], cwd: Path | None = None,
) -> list[dict[str, Any]]:
    """Find all code-entity nodes whose path matches the given files."""
    if not file_paths or conn is None:
        return []

    nodes: list[dict[str, Any]] = []
    seen_uids: set[str] = set()
    query_paths = _query_path_list(file_paths, cwd)
    path_list = ", ".join(f"'{p}'" for p in query_paths)

    for table in _CODE_NODE_TABLES:
        query = (
            f"MATCH (n:`{table}`) "
            f"WHERE n.path IN [{path_list}] "
            f"RETURN n.uid AS uid, n.name AS name, n.path AS path, "
            f"n.line_number AS line_number, label(n) AS kind"
        )
        try:
            result = conn.execute(query)
            while result.has_next():
                row = result.get_next()
                uid = row[0]
                if uid and uid not in seen_uids:
                    seen_uids.add(uid)
                    nodes.append({
                        "uid": uid,
                        "name": row[1],
                        "file": (
                            f"{_display_path(row[2], cwd)}:{row[3]}"
                            if row[2] and row[3]
                            else _display_path(row[2], cwd)
                        ),
                        "kind": row[4],
                    })
        except Exception:
            continue

    return nodes


def _single_hop_callers(
    conn: Any, target_uids: set[str], exclude_uids: set[str],
) -> list[dict[str, Any]]:
    """One-hop: find nodes that CALL into target_uids, excluding exclude_uids."""
    if not target_uids or conn is None:
        return []

    uid_list = ", ".join(f"'{u}'" for u in target_uids)
    exclude_list = ", ".join(f"'{u}'" for u in exclude_uids) if exclude_uids else "''"
    callers: list[dict[str, Any]] = []

    query = (
        f"MATCH (caller)-[r:CALLS]->(target) "
        f"WHERE target.uid IN [{uid_list}] AND NOT caller.uid IN [{exclude_list}] "
        f"RETURN DISTINCT caller.uid AS uid, caller.name AS name, "
        f"caller.path AS path, caller.line_number AS line_number, "
        f"label(caller) AS kind"
    )
    try:
        result = conn.execute(query)
        while result.has_next():
            row = result.get_next()
            uid = row[0]
            if uid:
                callers.append({
                    "uid": uid,
                    "name": row[1],
                    "file": f"{row[2]}:{row[3]}" if row[2] and row[3] else row[2],
                    "kind": row[4],
                })
    except Exception:
        pass

    return callers


def _single_hop_callees(
    conn: Any, source_uids: set[str], exclude_uids: set[str],
) -> list[dict[str, Any]]:
    """One-hop: find nodes that source_uids CALL, excluding exclude_uids."""
    if not source_uids or conn is None:
        return []

    uid_list = ", ".join(f"'{u}'" for u in source_uids)
    exclude_list = ", ".join(f"'{u}'" for u in exclude_uids) if exclude_uids else "''"
    callees: list[dict[str, Any]] = []

    query = (
        f"MATCH (source)-[r:CALLS]->(callee) "
        f"WHERE source.uid IN [{uid_list}] AND NOT callee.uid IN [{exclude_list}] "
        f"RETURN DISTINCT callee.uid AS uid, callee.name AS name, "
        f"callee.path AS path, callee.line_number AS line_number, "
        f"label(callee) AS kind"
    )
    try:
        result = conn.execute(query)
        while result.has_next():
            row = result.get_next()
            uid = row[0]
            if uid:
                callees.append({
                    "uid": uid,
                    "name": row[1],
                    "file": f"{row[2]}:{row[3]}" if row[2] and row[3] else row[2],
                    "kind": row[4],
                })
    except Exception:
        pass

    return callees


_DEFAULT_DEPTH = 5


def _find_callers_outside(
    conn: Any, scope_uids: set[str], *, depth: int = _DEFAULT_DEPTH,
) -> list[dict[str, Any]]:
    """Transitive: BFS callers up to `depth` hops outside the scope."""
    if not scope_uids or conn is None:
        return []

    all_callers: dict[str, dict[str, Any]] = {}
    frontier = set(scope_uids)
    visited = set(scope_uids)

    for _ in range(depth):
        hop = _single_hop_callers(conn, frontier, visited)
        if not hop:
            break
        frontier = set()
        for node in hop:
            uid = node["uid"]
            if uid not in visited:
                visited.add(uid)
                frontier.add(uid)
                if uid not in all_callers:
                    all_callers[uid] = node

    return list(all_callers.values())


def _find_callees_outside(
    conn: Any, scope_uids: set[str], *, depth: int = _DEFAULT_DEPTH,
) -> list[dict[str, Any]]:
    """Transitive: BFS callees up to `depth` hops outside the scope."""
    if not scope_uids or conn is None:
        return []

    all_callees: dict[str, dict[str, Any]] = {}
    frontier = set(scope_uids)
    visited = set(scope_uids)

    for _ in range(depth):
        hop = _single_hop_callees(conn, frontier, visited)
        if not hop:
            break
        frontier = set()
        for node in hop:
            uid = node["uid"]
            if uid not in visited:
                visited.add(uid)
                frontier.add(uid)
                if uid not in all_callees:
                    all_callees[uid] = node

    return list(all_callees.values())


def _find_cross_module_impact(
    conn: Any, file_paths: list[str], cwd: Path | None = None,
) -> list[str]:
    """Find modules imported by the given files that are outside the file set."""
    if not file_paths or conn is None:
        return []

    query_paths = _query_path_list(file_paths, cwd)
    path_list = ", ".join(f"'{p}'" for p in query_paths)
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
            mod = row[0]
            if mod:
                modules.add(mod.split(".")[0])
    except Exception:
        pass

    return sorted(modules)


def _count_in_degree(conn: Any, uids: list[str]) -> dict[str, int]:
    """Count incoming CALLS edges for each uid (for truncation ranking)."""
    if not uids or conn is None:
        return {}

    uid_list = ", ".join(f"'{u}'" for u in uids)
    degrees: dict[str, int] = {}

    query = (
        f"MATCH (caller)-[r:CALLS]->(target) "
        f"WHERE target.uid IN [{uid_list}] "
        f"RETURN target.uid AS uid, count(caller) AS deg"
    )
    try:
        result = conn.execute(query)
        while result.has_next():
            row = result.get_next()
            degrees[row[0]] = row[1]
    except Exception:
        pass

    return degrees


def _truncate_by_degree(
    nodes: list[dict[str, Any]],
    conn: Any,
    max_nodes: int,
) -> list[dict[str, Any]]:
    """Truncate a node list to max_nodes, preferring high-fan-in nodes."""
    if len(nodes) <= max_nodes:
        return nodes

    uids = [n["uid"] for n in nodes]
    degrees = _count_in_degree(conn, uids)
    nodes_sorted = sorted(nodes, key=lambda n: degrees.get(n["uid"], 0), reverse=True)
    return nodes_sorted[:max_nodes]


# ---------------------------------------------------------------------------
# Lock overlap detection
# ---------------------------------------------------------------------------

def _detect_lock_overlaps(
    callers: list[dict[str, Any]],
    callees: list[dict[str, Any]],
    locks: dict[str, list[str]],
    own_lane: str | None,
) -> list[dict[str, Any]]:
    """Detect which transitive callers/callees fall into another lane's locks.

    Args:
        callers: transitive callers outside scope
        callees: transitive callees outside scope
        locks: {lane_id: [file_path, ...]} from btrain lock table
        own_lane: current lane ID (excluded from overlap check)

    Returns:
        list of overlap records, one per overlapping lane.
    """
    if not locks:
        return []

    # Build a map: file_path -> set of lanes that lock it
    path_to_lanes: dict[str, set[str]] = {}
    for lane_id, lane_files in locks.items():
        if lane_id == own_lane:
            continue
        if not isinstance(lane_files, list):
            continue
        for f in lane_files:
            if isinstance(f, str):
                path_to_lanes.setdefault(f, set()).add(lane_id)

    if not path_to_lanes:
        return []

    # Check each caller/callee's file against the lock table
    overlap_by_lane: dict[str, dict[str, Any]] = {}
    all_nodes = [("caller", n) for n in callers] + [("callee", n) for n in callees]

    for role, node in all_nodes:
        node_file = node.get("file", "") or ""
        # Strip line number suffix for path matching
        file_path = node_file.split(":")[0] if ":" in node_file else node_file
        if not file_path:
            continue

        for locked_path, lanes in path_to_lanes.items():
            # Check if the node's file matches or is under a locked directory
            if file_path == locked_path or file_path.startswith(locked_path.rstrip("/") + "/"):
                for lane_id in lanes:
                    if lane_id not in overlap_by_lane:
                        overlap_by_lane[lane_id] = {
                            "lane": lane_id,
                            "overlapping_files": set(),
                            "overlapping_nodes": [],
                            "level": "warn",
                        }
                    overlap_by_lane[lane_id]["overlapping_files"].add(file_path)
                    overlap_by_lane[lane_id]["overlapping_nodes"].append({
                        "uid": node["uid"],
                        "name": node["name"],
                        "file": node.get("file"),
                        "kind": node.get("kind"),
                        "role": role,
                    })

    # Serialize sets to sorted lists
    overlaps = []
    for rec in sorted(overlap_by_lane.values(), key=lambda r: r["lane"]):
        overlaps.append({
            "lane": rec["lane"],
            "overlapping_files": sorted(rec["overlapping_files"]),
            "overlapping_nodes": rec["overlapping_nodes"],
            "level": rec["level"],
        })

    return overlaps


# ---------------------------------------------------------------------------
# Payload builder (pure, testable)
# ---------------------------------------------------------------------------

def build_blast_radius_payload(
    *,
    files: list[str],
    lane: str | None = None,
    locks_json: str | None = None,
    max_nodes: int = _DEFAULT_MAX_NODES,
    depth: int = _DEFAULT_DEPTH,
    conn: Any = None,
    cwd: Path | None = None,
) -> dict[str, Any]:
    """Build the blast-radius response payload.

    Args:
        files: list of file paths to expand through the graph.
        lane: current btrain lane ID (excluded from overlap check).
        locks_json: JSON string of {lane_id: [file_path, ...]} lock table.
        max_nodes: per-bucket cap for truncation.
        depth: max hops for transitive caller/callee BFS.
        conn: KùzuDB connection (may be None).
        cwd: working directory override for path resolution.
    """
    advisories: list[dict[str, str]] = []

    # Parse lock table
    locks: dict[str, list[str]] = {}
    if locks_json:
        try:
            parsed = json.loads(locks_json)
            if not isinstance(parsed, dict):
                raise ValueError("locks JSON must be an object")
            # Validate values: each must be a list of strings
            bad_keys = [
                k for k, v in parsed.items()
                if not isinstance(v, list) or not all(isinstance(f, str) for f in v)
            ]
            if bad_keys:
                advisories.append({
                    "level": "warn",
                    "kind": "invalid_locks_json",
                    "detail": (
                        f"Lock values for lane(s) {', '.join(bad_keys)} are not "
                        f"string arrays; those lanes excluded from overlap detection."
                    ),
                })
            locks = {
                k: v for k, v in parsed.items()
                if isinstance(v, list) and all(isinstance(f, str) for f in v)
            }
        except (json.JSONDecodeError, TypeError, ValueError):
            advisories.append({
                "level": "warn",
                "kind": "invalid_locks_json",
                "detail": "Could not parse --locks-json as an object; skipping overlap detection.",
            })

    # Find nodes in the requested file scope
    nodes_in_scope = _find_nodes_by_paths(conn, files, cwd)
    scope_uids = {n["uid"] for n in nodes_in_scope}

    # Expand: find transitive callers/callees outside the scope
    callers = _find_callers_outside(conn, scope_uids, depth=depth)
    callees = _find_callees_outside(conn, scope_uids, depth=depth)

    # Cross-module impact
    cross_module = _find_cross_module_impact(conn, files, cwd)

    # Truncation tracking
    total_scope = len(nodes_in_scope)
    total_callers = len(callers)
    total_callees = len(callees)
    truncated = False

    nodes_in_scope = _truncate_by_degree(nodes_in_scope, conn, max_nodes)
    callers = _truncate_by_degree(callers, conn, max_nodes)
    callees = _truncate_by_degree(callees, conn, max_nodes)

    if (len(nodes_in_scope) < total_scope
            or len(callers) < total_callers
            or len(callees) < total_callees):
        truncated = True
        advisories.append({
            "level": "info",
            "kind": "truncated",
            "detail": (
                f"Results truncated to {max_nodes} per bucket. "
                f"Total: {total_scope} in-scope, {total_callers} callers, "
                f"{total_callees} callees."
            ),
        })

    # Detect lock overlaps
    overlaps = _detect_lock_overlaps(callers, callees, locks, lane)
    if overlaps:
        for overlap in overlaps:
            advisories.append({
                "level": "warn",
                "kind": "lock_overlap",
                "detail": (
                    f"Lane {overlap['lane']} locks files that contain "
                    f"{len(overlap['overlapping_nodes'])} transitive "
                    f"caller(s)/callee(s) of your scope: "
                    f"{', '.join(overlap['overlapping_files'][:5])}"
                ),
            })

    # No graph data advisory
    if conn is None:
        advisories.append({
            "level": "warn",
            "kind": "no_graph",
            "detail": "KùzuDB unavailable; blast radius computed without graph data.",
        })

    payload: dict[str, Any] = {
        "ok": True,
        "kind": "blast_radius",
        "files": files,
        "nodes_in_scope": nodes_in_scope,
        "transitive_callers": callers,
        "transitive_callees": callees,
        "cross_module_impact": cross_module,
        "lock_overlaps": overlaps,
        "advisories": advisories,
        "summary": {
            "files_requested": len(files),
            "nodes_in_scope": len(nodes_in_scope),
            "transitive_callers": len(callers),
            "transitive_callees": len(callees),
            "lock_overlaps": len(overlaps),
        },
    }

    if truncated:
        payload["truncated"] = True
        payload["total_nodes"] = {
            "in_scope": total_scope,
            "callers": total_callers,
            "callees": total_callees,
        }

    return payload


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------

def blast_radius_command(
    files: str = typer.Option(
        ...,
        "--files",
        help="Comma-separated file paths to expand (e.g. from btrain lock list).",
    ),
    lane: Optional[str] = typer.Option(
        None,
        "--lane",
        help="Current btrain lane ID (excluded from overlap detection).",
    ),
    locks_json: Optional[str] = typer.Option(
        None,
        "--locks-json",
        help='JSON object of lane locks: {"lane_id": ["file", ...], ...}.',
    ),
    max_nodes: int = typer.Option(
        _DEFAULT_MAX_NODES,
        "--max-nodes",
        min=1,
        help="Per-bucket node cap (default 50).",
    ),
    depth: int = typer.Option(
        _DEFAULT_DEPTH,
        "--depth",
        min=1,
        help="Max hops for transitive caller/callee BFS (default 5).",
    ),
    project: Optional[str] = typer.Option(
        None,
        "--project",
        help=PROJECT_OPTION_HELP,
    ),
) -> None:
    """Pre-lock collision check: expand file paths through the code graph.

    Reports transitive callers and callees outside the requested scope,
    cross-module impact, and overlap with other active btrain lanes.
    """
    activate_project(project)
    file_list = [f.strip() for f in files.split(",") if f.strip()]
    if not file_list:
        typer.echo(emit_json({
            "ok": False,
            "kind": "no_files",
            "detail": "--files must contain at least one path.",
        }))
        raise typer.Exit(code=1)

    conn = None
    try:
        conn = get_kuzu_connection()
    except Exception as exc:
        print(
            f"Warning: KùzuDB unavailable ({exc}); graph data will be empty.",
            file=sys.stderr,
        )

    payload = build_blast_radius_payload(
        files=file_list,
        lane=lane,
        locks_json=locks_json,
        max_nodes=max_nodes,
        depth=depth,
        conn=conn,
    )

    typer.echo(emit_json(payload))
    raise typer.Exit(code=0)
