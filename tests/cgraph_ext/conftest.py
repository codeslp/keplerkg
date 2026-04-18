"""Shared fixtures and helpers for cgraph extension tests."""

import json
import sys
from pathlib import Path
from typing import Any

import typer

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


# ---------------------------------------------------------------------------
# KùzuDB mock helpers — used wherever a command reads from kuzu.
# ---------------------------------------------------------------------------


class FakeResult:
    """Iterable mimic of kuzu.QueryResult — has_next / get_next over a row list."""

    def __init__(self, rows):
        self._rows = list(rows)
        self._idx = 0

    def has_next(self):
        return self._idx < len(self._rows)

    def get_next(self):
        row = self._rows[self._idx]
        self._idx += 1
        return row


class FunctionOnlyConn:
    """Mock kuzu.Connection that returns `function_rows` for the first query
    mentioning the Function table and empty results for everything else.

    Matches the shape of `fetch_embedded_nodes`, which queries each
    EMBEDDABLE_TABLE exactly once per call.  Tests needing per-table responses
    should instantiate their own ad-hoc connection.
    """

    def __init__(self, function_rows):
        self._function_rows = list(function_rows)
        self._served = False

    def execute(self, query, **_kwargs):
        if "`Function`" in query and not self._served:
            self._served = True
            return FakeResult(self._function_rows)
        return FakeResult([])


# ---------------------------------------------------------------------------
# Typer app builder — wraps register_extensions with the required root callback.
# ---------------------------------------------------------------------------


def build_ext_app() -> typer.Typer:
    """Build a Typer app with cgraph extension commands registered under a root."""
    from codegraphcontext_ext.cli import register_extensions

    app = typer.Typer()

    @app.callback()
    def _root() -> None:
        return None

    register_extensions(app)
    return app


# ---------------------------------------------------------------------------
# CLI output helpers.
# ---------------------------------------------------------------------------


def extract_last_json(output: str) -> dict:
    """Extract the last `{...}` object from a CLI output blob (stdout + stderr)."""
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise ValueError(f"No JSON found in output: {output!r}")


# ---------------------------------------------------------------------------
# Backend probe fixture — flips on the kuzu-available gate without real DB.
# ---------------------------------------------------------------------------


def mark_kuzu_backend_available(monkeypatch) -> None:
    """Make probe_backend_support report kuzu-available for the current test."""
    from codegraphcontext_ext.embeddings import runtime

    monkeypatch.setenv("DEFAULT_DATABASE", "kuzudb")
    monkeypatch.setattr(runtime, "is_kuzudb_available", lambda: True)
