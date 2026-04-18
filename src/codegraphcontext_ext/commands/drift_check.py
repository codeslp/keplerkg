"""kkg drift-check: detect graph-neighborhood changes outside a lane.

Spec §4: given a lane's locked files, queries the graph for their
callers/callees/imports, then checks ``git log --since`` for commits
touching those neighbors.  Reports which graph nodes drifted so the
lane owner knows upstream shifted under them.

Timeout budget: 2s (adapter-enforced; this command does not self-enforce).
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import typer

from ..io.json_stdout import emit_json
from ..io.kuzu import get_kuzu_connection

COMMAND_NAME = "drift-check"
SCHEMA_FILE = "drift-check.json"
SUMMARY = "Detect graph-neighborhood changes outside a lane's locked files."

_CODE_NODE_TABLES = (
    "Function", "Class", "Variable", "Trait", "Interface",
    "Macro", "Struct", "Enum", "Union", "Annotation", "Record", "Property",
)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _git_changed_files(since: str, cwd: Path | None = None) -> list[str]:
    """Return files changed in commits since *since* (ISO timestamp)."""
    try:
        out = subprocess.check_output(
            ["git", "log", f"--since={since}", "--name-only", "--pretty=format:"],
            text=True,
            cwd=str(cwd or Path.cwd()),
            timeout=5,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return []
    return sorted({f.strip() for f in out.splitlines() if f.strip()})


def _repo_root(cwd: Path | None = None) -> Path:
    return (cwd or Path.cwd()).resolve()


def _rel_path(path: str, root: Path) -> str:
    """Best-effort relative path from repo root."""
    try:
        return str(Path(path).resolve().relative_to(root))
    except ValueError:
        return path


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------

def _find_nodes_for_files(conn: Any, files: list[str]) -> list[dict[str, str]]:
    """Find graph nodes whose ``file`` field matches any of *files*."""
    nodes: list[dict[str, str]] = []
    if not files:
        return nodes

    path_set = set()
    root = _repo_root()
    for f in files:
        path_set.add(f)
        path_set.add(str((root / f).resolve()))

    path_list = ", ".join(f"'{p}'" for p in sorted(path_set))

    for table in _CODE_NODE_TABLES:
        try:
            result = conn.execute(
                f"MATCH (n:{table}) WHERE n.file IN [{path_list}] "
                f"RETURN n.uid AS uid, n.name AS name, n.file AS file, '{table}' AS kind"
            )
        except Exception:
            continue
        while result.has_next():
            row = result.get_next()
            nodes.append({
                "uid": str(row[0]),
                "name": str(row[1]) if row[1] else None,
                "file": str(row[2]) if row[2] else None,
                "kind": str(row[3]),
            })
    return nodes


def _find_neighbors(conn: Any, uids: list[str]) -> list[dict[str, str]]:
    """Find 1-hop CALLS/IMPORTS neighbors of *uids* that are NOT in *uids*."""
    if not uids:
        return []
    uid_set = set(uids)
    neighbors: list[dict[str, str]] = []
    seen: set[str] = set()

    uid_list = ", ".join(f"'{u}'" for u in sorted(uid_set))

    for rel in ("CALLS", "IMPORTS"):
        # Outgoing
        for table in _CODE_NODE_TABLES:
            try:
                result = conn.execute(
                    f"MATCH (a)-[:{rel}]->(b:{table}) "
                    f"WHERE a.uid IN [{uid_list}] AND NOT b.uid IN [{uid_list}] "
                    f"RETURN b.uid AS uid, b.name AS name, b.file AS file, '{table}' AS kind"
                )
            except Exception:
                continue
            while result.has_next():
                row = result.get_next()
                uid = str(row[0])
                if uid not in seen:
                    seen.add(uid)
                    neighbors.append({
                        "uid": uid,
                        "name": str(row[1]) if row[1] else None,
                        "file": str(row[2]) if row[2] else None,
                        "kind": str(row[3]),
                    })
        # Incoming
        for table in _CODE_NODE_TABLES:
            try:
                result = conn.execute(
                    f"MATCH (a:{table})-[:{rel}]->(b) "
                    f"WHERE b.uid IN [{uid_list}] AND NOT a.uid IN [{uid_list}] "
                    f"RETURN a.uid AS uid, a.name AS name, a.file AS file, '{table}' AS kind"
                )
            except Exception:
                continue
            while result.has_next():
                row = result.get_next()
                uid = str(row[0])
                if uid not in seen:
                    seen.add(uid)
                    neighbors.append({
                        "uid": uid,
                        "name": str(row[1]) if row[1] else None,
                        "file": str(row[2]) if row[2] else None,
                        "kind": str(row[3]),
                    })
    return neighbors


# ---------------------------------------------------------------------------
# Payload builder (pure-ish, testable)
# ---------------------------------------------------------------------------

def build_drift_check_payload(
    files: list[str],
    since: str,
    lane: str | None = None,
) -> dict[str, Any]:
    """Build the drift-check response payload.

    1. Find graph nodes for locked files.
    2. Find their 1-hop neighbors (callers/callees/imports).
    3. Check git log --since for commits touching neighbor files.
    4. Report which neighbors drifted.
    """
    advisories: list[dict[str, str]] = []
    drifted: list[dict[str, Any]] = []
    neighbor_files: list[str] = []
    nodes_in_scope: list[dict[str, str]] = []
    neighbors: list[dict[str, str]] = []

    try:
        conn = get_kuzu_connection()
    except (SystemExit, Exception):
        advisories.append({
            "level": "warn",
            "kind": "no_graph",
            "detail": "Could not connect to KùzuDB; drift check skipped.",
        })
        return {
            "ok": True,
            "kind": "drift_check",
            "lane": lane,
            "since": since,
            "files": files,
            "nodes_in_scope": [],
            "neighbors": [],
            "drifted": [],
            "advisories": advisories,
        }

    nodes_in_scope = _find_nodes_for_files(conn, files)
    uids = [n["uid"] for n in nodes_in_scope]
    neighbors = _find_neighbors(conn, uids)

    # Collect unique files from neighbors (excluding locked files)
    locked_set = set(files)
    root = _repo_root()
    for f in list(locked_set):
        locked_set.add(str((root / f).resolve()))

    neighbor_file_set: set[str] = set()
    for n in neighbors:
        if n["file"] and n["file"] not in locked_set:
            rel = _rel_path(n["file"], root)
            neighbor_file_set.add(rel)

    neighbor_files = sorted(neighbor_file_set)

    # Check git for changes
    changed_files = set(_git_changed_files(since))

    for n in neighbors:
        if not n["file"]:
            continue
        rel = _rel_path(n["file"], root)
        if rel in changed_files:
            drifted.append({
                "uid": n["uid"],
                "name": n["name"],
                "file": rel,
                "kind": n["kind"],
            })

    return {
        "ok": True,
        "kind": "drift_check",
        "lane": lane,
        "since": since,
        "files": files,
        "nodes_in_scope": nodes_in_scope,
        "neighbors": neighbors,
        "neighbor_files": neighbor_files,
        "drifted": drifted,
        "advisories": advisories,
    }


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------

def drift_check_command(
    files: str = typer.Option(
        ...,
        "--files",
        help="Comma-separated list of locked file paths to check.",
    ),
    since: Optional[str] = typer.Option(
        None,
        "--since",
        help="ISO timestamp (e.g. 2026-04-17T12:00Z). Defaults to 24h ago.",
    ),
    lane: Optional[str] = typer.Option(
        None,
        "--lane",
        help="btrain lane id (for output labeling).",
    ),
) -> None:
    """Check if graph neighbors of locked files have changed since a timestamp."""
    file_list = [f.strip() for f in files.split(",") if f.strip()]

    if not since:
        # Default: 24 hours ago
        ts = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        since = ts.isoformat()

    payload = build_drift_check_payload(file_list, since, lane)
    typer.echo(emit_json(payload))
    raise typer.Exit(code=0)
