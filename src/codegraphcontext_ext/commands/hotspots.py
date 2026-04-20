"""kkg hotspots: git churn x graph centrality risk analysis.

Phase 7+ — Identifies high-risk code by combining git change frequency
(churn) with graph in-degree centrality (how many callers/importers).
Files that change often AND are heavily depended on are prime candidates
for refactoring, extra test coverage, or careful review.

Risk score = churn * centrality (normalized to 0-100 for the top entry).

Usage:
    kkg hotspots
    kkg hotspots --project flask --top 20
    kkg hotspots --since 30   # only count commits from last 30 days
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections import defaultdict
from typing import Any, Optional

import typer

from ..config import resolve_cgraph_config
from ..io.json_stdout import emit_json, make_envelope
from ..io.kuzu import get_kuzu_connection
from ..project import PROJECT_OPTION_HELP, _normalize_slug, activate_project

COMMAND_NAME = "hotspots"
SCHEMA_FILE = "hotspots.json"
SUMMARY = "Identify high-risk code via git churn x graph centrality analysis."


def _query_centrality(conn) -> dict:
    """Get in-degree centrality for Function and Class nodes.

    Returns {file_path: {name: in_degree, ...}, ...} grouped by file.
    Also returns per-symbol details.
    """
    symbols = []
    file_centrality = defaultdict(int)

    for table in ("Function", "Class"):
        try:
            result = conn.execute(
                f"MATCH (caller)-[:CALLS]->(n:{table}) "
                f"RETURN n.uid AS uid, n.name AS name, n.path AS path, "
                f"count(caller) AS in_degree "
                f"ORDER BY in_degree DESC"
            )
            while result.has_next():
                row = result.get_next()
                uid, name, path, in_degree = row[0], row[1], row[2], row[3]
                symbols.append({
                    "uid": uid or "",
                    "name": name or "",
                    "path": path or "",
                    "type": table,
                    "in_degree": in_degree,
                })
                if path:
                    file_centrality[path] += in_degree
        except Exception:
            continue

    return {"symbols": symbols, "file_centrality": dict(file_centrality)}


def _query_git_churn(since_days: int = 90, cwd: Optional[str] = None) -> dict:
    """Count commits per file from git log.

    When *cwd* is given, git runs in that directory so the churn reflects
    the target project's history rather than the tool repo's.

    Returns {file_path: commit_count, ...}.
    """
    churn = defaultdict(int)
    try:
        args = ["git", "log", "--format=", "--name-only"]
        if since_days > 0:
            args.append(f"--since={since_days} days ago")
        kwargs: dict = {"stderr": subprocess.DEVNULL, "text": True}
        if cwd is not None:
            kwargs["cwd"] = cwd
        output = subprocess.check_output(args, **kwargs)
        for line in output.strip().splitlines():
            line = line.strip()
            if line:
                churn[line] += 1
    except Exception:
        pass
    return dict(churn)


# ---------------------------------------------------------------------------
# Payload builder (pure-ish, testable)
# ---------------------------------------------------------------------------

def build_hotspots_payload(
    *,
    project: Optional[str] = None,
    top: int = 15,
    since_days: int = 90,
    conn,
    churn_override: Optional[dict] = None,
    source_checkout: Optional[str] = None,
) -> dict:
    """Build the hotspots payload from git churn and graph centrality.

    *conn* is a live KùzuDB connection obtained by the caller (typically
    via ``get_kuzu_connection()`` in the CLI path, which runs the Phase
    1.5 storage preflight).  *churn_override* enables testing without git.

    *source_checkout* is the path to the target project's git checkout.
    When set, git churn comes from that directory. When a *project* is
    given but *source_checkout* is None, churn is suppressed to avoid
    silently reading the caller repo's history from the current working
    directory.
    """
    since_days = max(0, since_days)

    centrality_data = _query_centrality(conn)
    file_centrality = centrality_data["file_centrality"]
    symbols = centrality_data["symbols"]

    if churn_override is not None:
        churn = churn_override
    elif project is not None and source_checkout is None:
        churn = {}
    else:
        churn = _query_git_churn(since_days, cwd=source_checkout)

    # Compute per-file risk scores
    all_files = set(file_centrality.keys()) | set(churn.keys())
    file_scores = []
    for fpath in all_files:
        c = churn.get(fpath, 0)
        d = file_centrality.get(fpath, 0)
        raw_score = c * d
        if raw_score > 0:
            file_scores.append({
                "path": fpath,
                "churn": c,
                "centrality": d,
                "raw_score": raw_score,
            })

    # Sort by raw score descending
    file_scores.sort(key=lambda x: x["raw_score"], reverse=True)

    # Normalize to 0-100
    max_raw = file_scores[0]["raw_score"] if file_scores else 1
    for entry in file_scores:
        entry["risk_score"] = round(entry["raw_score"] / max_raw * 100, 1)

    hotspots = file_scores[:top]

    return make_envelope("hotspots", {
        "hotspots": hotspots,
        "stats": {
            "files_analyzed": len(all_files),
            "symbols_analyzed": len(symbols),
            "since_days": since_days,
            "max_churn": max(churn.values()) if churn else 0,
            "max_centrality": max(file_centrality.values()) if file_centrality else 0,
        },
    }, project=project)


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------

def hotspots_command(
    project: Optional[str] = typer.Option(
        None,
        "--project",
        help=PROJECT_OPTION_HELP,
    ),
    top: int = typer.Option(
        15,
        "--top",
        help="Number of top hotspots to return.",
    ),
    since: int = typer.Option(
        90,
        "--since",
        help="Only count git commits from the last N days.",
    ),
) -> None:
    """Identify high-risk code via git churn x graph centrality."""
    target = activate_project(project)

    # get_kuzu_connection() runs require_storage() internally.
    # If storage is offline, it prints the storage_offline JSON and
    # raises SystemExit — preserving the Phase 1.5 contract.
    conn = get_kuzu_connection()

    # Resolve source checkout so git churn comes from the target
    # project, not the tool repo's cwd.  The config's source_checkout
    # is specific to the local project; only use it when the target
    # matches (i.e. was resolved from config/basename, not an explicit
    # --project pointing at a different repo).
    cfg = resolve_cgraph_config()
    source_checkout = None
    if cfg.source_checkout and cfg.source_checkout.is_dir():
        if target.source != "cli":
            source_checkout = str(cfg.source_checkout)
        else:
            try:
                checkout_slug = _normalize_slug(cfg.source_checkout.name)
            except ValueError:
                checkout_slug = ""
            if checkout_slug == target.slug:
                source_checkout = str(cfg.source_checkout)

    payload = build_hotspots_payload(
        project=target.slug,
        top=top,
        since_days=max(0, since),
        conn=conn,
        source_checkout=source_checkout,
    )

    # Human-readable summary to stderr
    hotspots = payload.get("hotspots", [])
    stats = payload.get("stats", {})
    print(
        f"Hotspots: {len(hotspots)} results from "
        f"{stats.get('files_analyzed', 0)} files, "
        f"{stats.get('symbols_analyzed', 0)} symbols "
        f"(last {stats.get('since_days', 90)} days)",
        file=sys.stderr,
    )
    if hotspots:
        top_entry = hotspots[0]
        print(
            f"  #1: {top_entry['path']} "
            f"(churn={top_entry['churn']}, centrality={top_entry['centrality']}, "
            f"risk={top_entry['risk_score']})",
            file=sys.stderr,
        )

    typer.echo(emit_json(payload))
    raise typer.Exit(code=0 if payload.get("ok") else 1)
