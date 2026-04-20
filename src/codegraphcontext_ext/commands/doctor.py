"""kkg doctor — validate setup, backend, graph, and embeddings.

Runs a series of diagnostic checks and reports pass/fail for each.
Designed to surface every onboarding blocker in one command.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Optional

import typer

from ..io.json_stdout import emit_json

SUMMARY = "Validate setup: backend, DB access, graph, embeddings, PATH."


def _check_cli_on_path() -> dict[str, Any]:
    """Check if kkg is on PATH."""
    kkg_path = shutil.which("kkg")
    return {
        "check": "cli_on_path",
        "ok": kkg_path is not None,
        "detail": f"kkg found at {kkg_path}" if kkg_path else "kkg not on PATH — activate the venv or add to PATH",
    }


def _check_backend_config() -> dict[str, Any]:
    """Check that the configured backend matches what the ext layer resolves."""
    config_db = None
    try:
        from codegraphcontext.cli.config_manager import get_config_value
        config_db = get_config_value("DEFAULT_DATABASE")
    except Exception:
        pass

    env_db = os.environ.get("DEFAULT_DATABASE") or os.environ.get("CGC_RUNTIME_DB_TYPE")

    from ..embeddings.runtime import resolve_requested_backend
    resolved = resolve_requested_backend()

    effective = env_db or config_db or "(auto-detected)"
    match = (config_db or "").lower() == resolved if config_db else True

    return {
        "check": "backend_config",
        "ok": match and resolved != "unavailable",
        "detail": f"config={config_db or '(none)'}, env={env_db or '(none)'}, resolved={resolved}",
        "resolved_backend": resolved,
    }


def _check_db_access() -> dict[str, Any]:
    """Check that KuzuDB is accessible and the database can be opened."""
    try:
        from ..io.kuzu import get_kuzu_connection
        conn = get_kuzu_connection()
        # Quick smoke test
        result = conn.execute("MATCH (f:File) RETURN count(f)")
        count = result.get_next()[0]
        return {
            "check": "db_access",
            "ok": True,
            "detail": f"KuzuDB connected, {count} files indexed",
            "file_count": count,
        }
    except Exception as exc:
        return {
            "check": "db_access",
            "ok": False,
            "detail": f"Cannot connect to KuzuDB: {exc}",
        }


def _check_graph_nodes() -> dict[str, Any]:
    """Check that the graph has nodes (index has been run)."""
    try:
        from ..io.kuzu import get_kuzu_connection
        conn = get_kuzu_connection()

        result = conn.execute("MATCH (f:Function) RETURN count(f)")
        func_count = result.get_next()[0]

        result = conn.execute("MATCH (c:Class) RETURN count(c)")
        class_count = result.get_next()[0]

        ok = func_count > 0
        return {
            "check": "graph_nodes",
            "ok": ok,
            "detail": f"{func_count} functions, {class_count} classes" if ok else "No functions indexed — run: kkg index",
            "function_count": func_count,
            "class_count": class_count,
        }
    except Exception as exc:
        return {
            "check": "graph_nodes",
            "ok": False,
            "detail": f"Cannot query graph: {exc}",
        }


def _check_calls_edges() -> dict[str, Any]:
    """Check that CALLS edges exist (critical for graph-based features)."""
    try:
        from ..io.kuzu import get_kuzu_connection
        conn = get_kuzu_connection()

        result = conn.execute("MATCH ()-[c:CALLS]->() RETURN count(c)")
        count = result.get_next()[0]

        if count == 0:
            return {
                "check": "calls_edges",
                "ok": False,
                "detail": (
                    "0 CALLS edges — blast-radius, impact, execution-flow, and fan-out "
                    "audit rules will return empty results. Enable SCIP indexing for call "
                    "extraction: set SCIP_INDEXER=true in .codegraphcontext/.env and re-run "
                    "kkg index --force"
                ),
                "edge_count": 0,
            }
        return {
            "check": "calls_edges",
            "ok": True,
            "detail": f"{count} CALLS edges",
            "edge_count": count,
        }
    except Exception:
        return {
            "check": "calls_edges",
            "ok": False,
            "detail": "Cannot query CALLS edges (DB not accessible)",
        }


def _check_embeddings() -> dict[str, Any]:
    """Check that embeddings have been computed."""
    try:
        from ..io.kuzu import get_kuzu_connection
        conn = get_kuzu_connection()

        result = conn.execute(
            "MATCH (f:Function) WHERE f.embedding IS NOT NULL RETURN count(f)"
        )
        count = result.get_next()[0]

        if count == 0:
            return {
                "check": "embeddings",
                "ok": False,
                "detail": "No embeddings found — run: kkg embed",
                "embedded_count": 0,
            }
        return {
            "check": "embeddings",
            "ok": True,
            "detail": f"{count} functions have embeddings",
            "embedded_count": count,
        }
    except Exception:
        return {
            "check": "embeddings",
            "ok": False,
            "detail": "Cannot check embeddings (DB not accessible)",
        }


def _check_storage() -> dict[str, Any]:
    """Check that storage paths are accessible."""
    from ..preflight import check_storage
    result = check_storage()
    if result is not None:
        return {
            "check": "storage",
            "ok": False,
            "detail": result.get("detail", "Storage offline"),
        }
    kuzu_path = os.environ.get("KUZUDB_PATH", "")
    if not kuzu_path:
        try:
            from codegraphcontext.cli.config_manager import get_config_value
            kuzu_path = get_config_value("KUZUDB_PATH") or ""
        except Exception:
            pass
    return {
        "check": "storage",
        "ok": True,
        "detail": f"KUZUDB_PATH={kuzu_path}" if kuzu_path else "Using default storage path",
    }


def doctor_command(
    project: Optional[str] = typer.Option(
        None, "--project",
        help="Target project slug.",
    ),
) -> None:
    """Run diagnostic checks and report setup health."""

    if project:
        from ..project import activate_project
        activate_project(project)

    checks = [
        _check_cli_on_path(),
        _check_backend_config(),
        _check_storage(),
        _check_db_access(),
        _check_graph_nodes(),
        _check_calls_edges(),
        _check_embeddings(),
    ]

    passed = sum(1 for c in checks if c["ok"])
    total = len(checks)
    all_ok = passed == total

    # Human-readable summary to stderr
    for c in checks:
        icon = "PASS" if c["ok"] else "FAIL"
        print(f"  [{icon}] {c['check']}: {c['detail']}", file=sys.stderr)

    print(f"\n  {passed}/{total} checks passed", file=sys.stderr)
    if not all_ok:
        print("  Fix the FAIL items above and re-run: kkg doctor", file=sys.stderr)

    emit_json({
        "ok": all_ok,
        "kind": "doctor",
        "checks": checks,
        "passed": passed,
        "total": total,
    })

    if not all_ok:
        raise SystemExit(1)
