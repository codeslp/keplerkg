"""Tests for the Phase 4 drift-check command."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import jsonschema
import pytest
from typer.testing import CliRunner

from codegraphcontext_ext.commands.drift_check import (
    COMMAND_NAME,
    SCHEMA_FILE,
    SUMMARY,
    build_drift_check_payload,
    _git_changed_files,
)
from codegraphcontext_ext.io.schema_check import schema_path

runner = CliRunner()


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def test_command_metadata():
    assert COMMAND_NAME == "drift-check"
    assert SCHEMA_FILE == "drift-check.json"
    assert isinstance(SUMMARY, str) and len(SUMMARY) > 0


# ---------------------------------------------------------------------------
# _git_changed_files
# ---------------------------------------------------------------------------

def test_git_changed_files_parses_output():
    mock_output = "src/auth.py\nsrc/utils.py\n\nsrc/auth.py\n"
    with patch(
        "codegraphcontext_ext.commands.drift_check.subprocess.check_output",
        return_value=mock_output,
    ):
        result = _git_changed_files("2026-04-17")
    assert result == ["src/auth.py", "src/utils.py"]


def test_git_changed_files_handles_failure():
    import subprocess
    with patch(
        "codegraphcontext_ext.commands.drift_check.subprocess.check_output",
        side_effect=subprocess.SubprocessError,
    ):
        result = _git_changed_files("2026-04-17")
    assert result == []


# ---------------------------------------------------------------------------
# Fake DB helpers
# ---------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)
        self._idx = 0

    def has_next(self):
        return self._idx < len(self._rows)

    def get_next(self):
        row = self._rows[self._idx]
        self._idx += 1
        return row


class _FakeConn:
    def __init__(self, queries=None):
        self._queries = queries or {}

    def execute(self, query):
        for key, rows in self._queries.items():
            if key in query:
                return _FakeResult(rows)
        return _FakeResult([])


# ---------------------------------------------------------------------------
# build_drift_check_payload
# ---------------------------------------------------------------------------

def test_no_graph_connection():
    """When DB is unavailable, returns no_graph advisory."""
    with patch(
        "codegraphcontext_ext.commands.drift_check.get_kuzu_connection",
        side_effect=Exception("no db"),
    ):
        result = build_drift_check_payload(["src/auth.py"], "2026-04-17", lane="a")
    assert result["ok"] is True
    assert result["kind"] == "drift_check"
    assert any(a["kind"] == "no_graph" for a in result["advisories"])
    assert result["drifted"] == []


def test_no_drift():
    """Neighbors exist but none changed → empty drifted list."""
    fake_conn = _FakeConn({
        "Function": [
            ("uid1", "login", "/repo/src/auth.py", "Function"),
        ],
        "CALLS": [
            ("uid2", "hash_password", "/repo/src/crypto.py", "Function"),
        ],
    })
    with patch(
        "codegraphcontext_ext.commands.drift_check.get_kuzu_connection",
        return_value=fake_conn,
    ), patch(
        "codegraphcontext_ext.commands.drift_check._git_changed_files",
        return_value=[],
    ):
        result = build_drift_check_payload(["src/auth.py"], "2026-04-17", lane="a")
    assert result["ok"] is True
    assert result["drifted"] == []


def test_drift_detected():
    """When a neighbor's file appears in git log, it's reported as drifted."""
    fake_conn = _FakeConn({
        "Function": [
            ("uid1", "login", "/repo/src/auth.py", "Function"),
        ],
        "CALLS": [
            ("uid2", "hash_password", "/repo/src/crypto.py", "Function"),
        ],
    })
    with patch(
        "codegraphcontext_ext.commands.drift_check.get_kuzu_connection",
        return_value=fake_conn,
    ), patch(
        "codegraphcontext_ext.commands.drift_check._git_changed_files",
        return_value=["src/crypto.py"],
    ), patch(
        "codegraphcontext_ext.commands.drift_check._rel_path",
        side_effect=lambda p, r: p.replace("/repo/", ""),
    ):
        result = build_drift_check_payload(["src/auth.py"], "2026-04-17", lane="a")
    assert len(result["drifted"]) >= 1
    assert any(d["file"] == "src/crypto.py" for d in result["drifted"])


def test_empty_files():
    """Empty file list → valid but empty payload."""
    with patch(
        "codegraphcontext_ext.commands.drift_check.get_kuzu_connection",
        side_effect=Exception("no db"),
    ):
        result = build_drift_check_payload([], "2026-04-17")
    assert result["ok"] is True
    assert result["nodes_in_scope"] == []


def test_lane_in_output():
    """Lane ID flows through to output."""
    with patch(
        "codegraphcontext_ext.commands.drift_check.get_kuzu_connection",
        side_effect=Exception("no db"),
    ):
        result = build_drift_check_payload(["a.py"], "2026-04-17", lane="b")
    assert result["lane"] == "b"


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def _load_schema():
    return json.loads(schema_path("drift-check.json").read_text())


def test_schema_validation_no_graph():
    with patch(
        "codegraphcontext_ext.commands.drift_check.get_kuzu_connection",
        side_effect=Exception("no db"),
    ):
        result = build_drift_check_payload(["src/auth.py"], "2026-04-17", lane="a")
    jsonschema.validate(result, _load_schema())


def test_schema_validation_with_drift():
    fake_conn = _FakeConn({
        "Function": [("uid1", "fn", "/repo/src/a.py", "Function")],
        "CALLS": [("uid2", "helper", "/repo/src/b.py", "Function")],
    })
    with patch(
        "codegraphcontext_ext.commands.drift_check.get_kuzu_connection",
        return_value=fake_conn,
    ), patch(
        "codegraphcontext_ext.commands.drift_check._git_changed_files",
        return_value=["src/b.py"],
    ), patch(
        "codegraphcontext_ext.commands.drift_check._rel_path",
        side_effect=lambda p, r: p.replace("/repo/", ""),
    ):
        result = build_drift_check_payload(["src/a.py"], "2026-04-17", lane="a")
    jsonschema.validate(result, _load_schema())


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def test_cli_basic():
    from codegraphcontext_ext.cli import register_extensions
    import typer

    app = typer.Typer()
    register_extensions(app)

    with patch(
        "codegraphcontext_ext.commands.drift_check.get_kuzu_connection",
        side_effect=Exception("no db"),
    ):
        result = runner.invoke(app, [
            "drift-check",
            "--files", "src/auth.py",
            "--since", "2026-04-17",
            "--lane", "a",
        ])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["kind"] == "drift_check"
    assert payload["lane"] == "a"


def test_cli_default_since():
    """When --since is omitted, a default timestamp is used."""
    from codegraphcontext_ext.cli import register_extensions
    import typer

    app = typer.Typer()
    register_extensions(app)

    with patch(
        "codegraphcontext_ext.commands.drift_check.get_kuzu_connection",
        side_effect=Exception("no db"),
    ):
        result = runner.invoke(app, ["drift-check", "--files", "src/auth.py"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["since"]  # Non-empty timestamp


def test_drift_check_registered():
    from codegraphcontext_ext.cli import register_extensions
    import typer

    app = typer.Typer()
    register_extensions(app)
    names = [cmd.name for cmd in app.registered_commands]
    assert "drift-check" in names
