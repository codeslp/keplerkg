"""kkg entrypoints: rank likely entry points from decorator metadata.

Phase 5.7 surfaces externally-invoked functions such as Flask/FastAPI
handlers, CLI commands, and pytest fixtures as a machine-readable CLI
command. Ranking is heuristic: decorator class first, incoming CALLS
second.

Phase 5.8: decorator classification now delegates to the shared
``framework.resolver`` so all commands and audit rules use the same
patterns.
"""

from __future__ import annotations

import sys
from typing import Any, Optional

import typer

from ..framework.resolver import classify_decorators
from ..io.json_stdout import emit_json, make_envelope
from ..io.kuzu import get_kuzu_connection
from ..project import PROJECT_OPTION_HELP, activate_project

COMMAND_NAME = "entrypoints"
SCHEMA_FILE = "entrypoints.json"
SUMMARY = "Score and rank code entities as entry points by decorators and in-degree."

_DEFAULT_LIMIT = 20
_DEFAULT_SCAN_LIMIT = 500


def _format_file(path: str | None, line_number: int | None) -> str | None:
    if path and line_number:
        return f"{path}:{line_number}"
    return path


def _fetch_candidate_functions(
    conn: Any,
    *,
    scan_limit: int = _DEFAULT_SCAN_LIMIT,
) -> list[dict[str, Any]]:
    """Fetch candidate Function nodes that may have entrypoint decorators."""
    query = (
        "MATCH (f:Function) "
        "WHERE NOT f.is_dependency "
        "RETURN f.uid AS uid, f.name AS name, f.path AS path, "
        "f.line_number AS line_number, f.decorators AS decorators "
        f"LIMIT {scan_limit}"
    )

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    result = conn.execute(query)
    while result.has_next():
        row = result.get_next()
        uid = row[0]
        if not uid or uid in seen:
            continue
        seen.add(uid)
        decorators = list(row[4]) if row[4] else []
        if not decorators:
            continue
        candidates.append({
            "uid": uid,
            "name": row[1],
            "file": _format_file(row[2], row[3]),
            "kind": "Function",
            "decorators": decorators,
        })
    return candidates


def _count_in_degree(conn: Any, uid: str) -> int:
    """Count incoming CALLS edges for a Function UID."""
    query = (
        "MATCH (f:Function) "
        "WHERE f.uid = $uid "
        "OPTIONAL MATCH (caller)-[:CALLS]->(f) "
        "RETURN count(caller) AS in_degree"
    )
    try:
        result = conn.execute(query, parameters={"uid": uid})
    except Exception:
        return 0
    if not result.has_next():
        return 0
    row = result.get_next()
    try:
        return int(row[0] or 0)
    except (TypeError, ValueError):
        return 0


def _score_entrypoint(base_score: float, in_degree: int) -> float:
    return round(base_score + min(in_degree, 20) * 0.25, 2)


def build_entrypoints_payload(
    *,
    limit: int = _DEFAULT_LIMIT,
    framework_filter: str | None = None,
    conn: Any = None,
    project: str | None = None,
) -> dict[str, Any]:
    """Build the ranked entrypoint payload."""
    normalized_filter = framework_filter.lower().strip() if framework_filter else None
    payload_data: dict[str, Any] = {
        "framework_filter": normalized_filter,
        "entrypoints": [],
        "advisories": [],
        "summary": {
            "total": 0,
            "limit": limit,
            "frameworks_detected": [],
        },
    }

    if conn is None:
        payload_data["advisories"].append({
            "level": "warn",
            "kind": "no_graph",
            "detail": "Could not connect to KuzuDB; entry-point scoring skipped.",
        })
        return make_envelope(COMMAND_NAME, payload_data, project=project)

    try:
        ranked: list[dict[str, Any]] = []
        for candidate in _fetch_candidate_functions(conn):
            match = classify_decorators(candidate["decorators"])
            if match is None:
                continue

            if normalized_filter and match.framework != normalized_filter:
                continue

            in_degree = _count_in_degree(conn, candidate["uid"])
            ranked.append({
                "uid": candidate["uid"],
                "name": candidate["name"],
                "file": candidate["file"],
                "kind": candidate["kind"],
                "score": _score_entrypoint(match.base_score, in_degree),
                "decorators": list(match.matched_decorators),
                "in_degree": in_degree,
                "framework": match.framework,
            })
    except Exception as exc:
        return make_envelope(
            COMMAND_NAME,
            payload_data,
            ok=False,
            error=f"Entrypoint scoring failed: {exc}",
            project=project,
        )

    ranked.sort(
        key=lambda item: (
            -item["score"],
            -item["in_degree"],
            item["name"] or "",
            item["uid"],
        )
    )

    total = len(ranked)
    if total > limit:
        payload_data["advisories"].append({
            "level": "info",
            "kind": "truncated",
            "detail": f"Results capped at {limit} ranked entry points.",
        })

    payload_data["entrypoints"] = ranked[:limit]
    payload_data["summary"] = {
        "total": total,
        "limit": limit,
        "frameworks_detected": sorted({
            entrypoint["framework"]
            for entrypoint in ranked
            if entrypoint.get("framework")
        }),
    }
    return make_envelope(COMMAND_NAME, payload_data, project=project)


def entrypoints_command(
    limit: int = typer.Option(
        _DEFAULT_LIMIT,
        "--limit",
        min=1,
        help="Maximum number of ranked entry points to return.",
    ),
    framework: Optional[str] = typer.Option(
        None,
        "--framework",
        help="Optional framework filter (flask, fastapi, cli, pytest).",
    ),
    project: Optional[str] = typer.Option(
        None,
        "--project",
        help=PROJECT_OPTION_HELP,
    ),
) -> None:
    """Score likely entry points from decorators and incoming CALLS edges."""
    target = activate_project(project)

    conn = None
    try:
        conn = get_kuzu_connection()
    except Exception as exc:
        print(
            f"Warning: KuzuDB unavailable ({exc}); entry-point scoring skipped.",
            file=sys.stderr,
        )

    payload = build_entrypoints_payload(
        limit=limit,
        framework_filter=framework,
        conn=conn,
        project=target.slug,
    )

    typer.echo(emit_json(payload))
    raise typer.Exit(code=0 if payload.get("ok") else 1)
