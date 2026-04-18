"""Tests for the Phase 5 kkg audit command."""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock, patch

import jsonschema
import pytest
from typer.testing import CliRunner

from codegraphcontext_ext.commands.audit import (
    COMMAND_NAME,
    SCHEMA_FILE,
    SUMMARY,
    build_audit_payload,
    build_list_payload,
    build_explain_payload,
)
from codegraphcontext_ext.io.schema_check import schema_path

runner = CliRunner()


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def test_command_metadata():
    assert COMMAND_NAME == "audit"
    assert SCHEMA_FILE == "audit.json"
    assert isinstance(SUMMARY, str)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_standards_dir(tmp_path):
    """Create a standards dir with one test rule."""
    std = tmp_path / "standards"
    std.mkdir()
    (std / "_exemptions.yaml").write_text("paths: []\n")
    (std / "test_rule.yaml").write_text(dedent("""\
        id: test_rule
        advisory_kind: test_kind
        severity: warn
        category: testing
        summary: Test rule
        query: "MATCH (f:Function) WHERE f.x > 5 RETURN f.uid, f.name, f.path, f.line_number, f.x AS metric"
        thresholds:
          warn: 5
        suggestion: "Fix {{name}}"
        evidence: "Test evidence"
    """))
    (std / "hard_rule.yaml").write_text(dedent("""\
        id: hard_rule
        advisory_kind: hard_kind
        severity: hard
        category: testing
        summary: Hard test rule
        query: "MATCH (f:Function) WHERE f.bad = true RETURN f.uid, f.name, f.path, f.line_number"
        evidence: "Hard evidence"
    """))
    return std


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
    def __init__(self, rows_by_query=None):
        self._rows_by_query = rows_by_query or {}

    def execute(self, query):
        for key, rows in self._rows_by_query.items():
            if key in query:
                return _FakeResult(rows)
        return _FakeResult([])


# ---------------------------------------------------------------------------
# build_audit_payload
# ---------------------------------------------------------------------------

def test_audit_no_violations(tmp_path):
    std = _make_standards_dir(tmp_path)
    conn = _FakeConn()
    with patch(
        "codegraphcontext_ext.commands.audit.get_kuzu_connection",
        return_value=conn,
    ):
        result = build_audit_payload(standards_dir=std)
    assert result["ok"] is True
    assert result["kind"] == "audit"
    assert result["standards_evaluated"] == 2
    assert result["advisories"] == []
    assert result["hard_zero"] is True


def test_audit_with_warn_violations(tmp_path):
    std = _make_standards_dir(tmp_path)
    conn = _FakeConn(rows_by_query={
        "f.x > 5": [("uid1", "bad_func", "src/a.py", 10, 25)],
    })
    with patch(
        "codegraphcontext_ext.commands.audit.get_kuzu_connection",
        return_value=conn,
    ):
        result = build_audit_payload(standards_dir=std)
    assert result["ok"] is True
    assert result["counts"]["warn"] == 1
    assert result["counts"]["hard"] == 0
    assert result["hard_zero"] is True


def test_audit_with_hard_violations(tmp_path):
    std = _make_standards_dir(tmp_path)
    conn = _FakeConn(rows_by_query={
        "f.bad = true": [("uid2", "bad_class", "src/b.py", 5)],
    })
    with patch(
        "codegraphcontext_ext.commands.audit.get_kuzu_connection",
        return_value=conn,
    ):
        result = build_audit_payload(standards_dir=std)
    assert result["counts"]["hard"] == 1
    assert result["hard_zero"] is False


def test_audit_db_unavailable(tmp_path):
    std = _make_standards_dir(tmp_path)
    with patch(
        "codegraphcontext_ext.commands.audit.get_kuzu_connection",
        side_effect=Exception("no db"),
    ):
        result = build_audit_payload(standards_dir=std)
    assert result["ok"] is False
    assert "error" in result


def test_audit_category_filter(tmp_path):
    std = _make_standards_dir(tmp_path)
    conn = _FakeConn()
    with patch(
        "codegraphcontext_ext.commands.audit.get_kuzu_connection",
        return_value=conn,
    ):
        result = build_audit_payload(standards_dir=std, category="nonexistent")
    assert result["standards_evaluated"] == 0


# ---------------------------------------------------------------------------
# build_list_payload
# ---------------------------------------------------------------------------

def test_list_payload(tmp_path):
    std = _make_standards_dir(tmp_path)
    result = build_list_payload(standards_dir=std)
    assert result["kind"] == "audit_list"
    assert len(result["standards"]) == 2
    ids = {s["id"] for s in result["standards"]}
    assert "test_rule" in ids
    assert "hard_rule" in ids


# ---------------------------------------------------------------------------
# build_explain_payload
# ---------------------------------------------------------------------------

def test_explain_found(tmp_path):
    std = _make_standards_dir(tmp_path)
    result = build_explain_payload("test_rule", standards_dir=std)
    assert result["kind"] == "audit_explain"
    assert result["id"] == "test_rule"
    assert result["severity"] == "warn"
    assert result["evidence"] == "Test evidence"


def test_explain_not_found(tmp_path):
    std = _make_standards_dir(tmp_path)
    result = build_explain_payload("nonexistent", standards_dir=std)
    assert "error" in result


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def _load_schema():
    return json.loads(schema_path("audit.json").read_text())


def test_schema_validation_clean(tmp_path):
    std = _make_standards_dir(tmp_path)
    conn = _FakeConn()
    with patch(
        "codegraphcontext_ext.commands.audit.get_kuzu_connection",
        return_value=conn,
    ):
        result = build_audit_payload(standards_dir=std)
    jsonschema.validate(result, _load_schema())


def test_schema_validation_with_violations(tmp_path):
    std = _make_standards_dir(tmp_path)
    conn = _FakeConn(rows_by_query={
        "f.x > 5": [("uid1", "fn", "a.py", 1, 20)],
        "f.bad = true": [("uid2", "cls", "b.py", 5)],
    })
    with patch(
        "codegraphcontext_ext.commands.audit.get_kuzu_connection",
        return_value=conn,
    ):
        result = build_audit_payload(standards_dir=std)
    jsonschema.validate(result, _load_schema())


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def test_cli_list():
    from codegraphcontext_ext.cli import register_extensions
    import typer

    app = typer.Typer()
    register_extensions(app)

    with patch(
        "codegraphcontext_ext.commands.audit._find_standards_dir",
        return_value=Path(__file__).parent.parent.parent / "standards",
    ):
        result = runner.invoke(app, ["audit", "--list"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["kind"] == "audit_list"


def test_audit_registered():
    from codegraphcontext_ext.cli import register_extensions
    import typer

    app = typer.Typer()
    register_extensions(app)
    names = [cmd.name for cmd in app.registered_commands]
    assert "audit" in names
