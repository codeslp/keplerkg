"""Tests for the kkg impact command.

Covers: symbol lookup, BFS caller/callee expansion, cross-module impact,
truncation, advisories, schema validation, and CLI wiring.
"""

import json
from pathlib import Path
from unittest.mock import patch

import jsonschema
from typer.testing import CliRunner

from codegraphcontext_ext.commands.impact import (
    COMMAND_NAME,
    SCHEMA_FILE,
    SUMMARY,
    _DEFAULT_DEPTH,
    _DEFAULT_MAX_NODES,
    build_impact_payload,
)

runner = CliRunner()


def _extract_json(output: str) -> dict:
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise ValueError(f"No JSON found in output: {output!r}")


# ---------------------------------------------------------------------------
# Fake KùzuDB connection
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
    """Mock KùzuDB connection for impact tests."""

    def __init__(self, symbols=None, callers=None, callees=None, imports=None):
        self._symbols = symbols or []
        self._callers = callers or []
        self._callees = callees or []
        self._imports = imports or []

    def execute(self, query, *, parameters=None):
        q = query.lower()
        # Symbol lookup: parameterized MATCH with n.name = $name
        if parameters and "name" in parameters:
            return _FakeResult(self._symbols)
        # Caller query
        if "match (caller)" in q and "calls" in q:
            return _FakeResult(self._callers)
        # Callee query
        if "match (source)" in q and "calls" in q:
            return _FakeResult(self._callees)
        # Import query
        if "imports" in q:
            return _FakeResult(self._imports)
        return _FakeResult([])


# ---------------------------------------------------------------------------
# Module metadata
# ---------------------------------------------------------------------------


def test_command_metadata():
    assert COMMAND_NAME == "impact"
    assert SCHEMA_FILE == "impact.json"
    assert isinstance(SUMMARY, str) and len(SUMMARY) > 0


def test_defaults():
    assert _DEFAULT_DEPTH == 3
    assert _DEFAULT_MAX_NODES == 50


# ---------------------------------------------------------------------------
# Payload builder — no DB
# ---------------------------------------------------------------------------


def test_no_conn_returns_advisory():
    payload = build_impact_payload(symbol="foo", conn=None)
    assert payload["ok"] is True
    assert payload["kind"] == "impact"
    assert payload["symbol"] == "foo"
    assert payload["matches"] == []
    assert payload["callers"] == []
    assert payload["callees"] == []
    kinds = {a["kind"] for a in payload["advisories"]}
    assert "no_graph" in kinds


def test_symbol_not_found_advisory():
    conn = _FakeConn(symbols=[])
    payload = build_impact_payload(symbol="nonexistent", conn=conn)
    kinds = {a["kind"] for a in payload["advisories"]}
    assert "symbol_not_found" in kinds


def test_basic_expansion():
    conn = _FakeConn(
        symbols=[("uid-auth", "authenticate", "src/auth.py", 10, "Function")],
        callers=[("uid-login", "login", "src/login.py", 5, "Function")],
        callees=[("uid-db", "query_db", "src/db.py", 20, "Function")],
        imports=[("flask",)],
    )
    payload = build_impact_payload(symbol="authenticate", conn=conn)

    assert payload["ok"] is True
    assert payload["kind"] == "impact"
    assert len(payload["matches"]) == 1
    assert payload["matches"][0]["uid"] == "uid-auth"
    assert len(payload["callers"]) == 1
    assert payload["callers"][0]["uid"] == "uid-login"
    assert payload["callers"][0]["hops"] == 1
    assert len(payload["callees"]) == 1
    assert payload["callees"][0]["uid"] == "uid-db"
    assert "flask" in payload["cross_module_impact"]
    assert payload["summary"]["matches"] == 1
    assert payload["summary"]["callers"] == 1
    assert payload["summary"]["callees"] == 1


def test_kind_filter_in_payload():
    conn = _FakeConn(symbols=[("uid-cls", "MyClass", "src/m.py", 1, "Class")])
    payload = build_impact_payload(symbol="MyClass", kind="Class", conn=conn)
    assert payload["kind_filter"] == "Class"


def test_truncation_advisory():
    conn = _FakeConn(
        symbols=[("uid-a", "a", "src/a.py", 1, "Function")],
        callers=[
            (f"uid-c{i}", f"caller{i}", f"src/c{i}.py", i, "Function")
            for i in range(60)
        ],
    )
    payload = build_impact_payload(
        symbol="a", conn=conn, max_nodes=5, depth=1,
    )
    assert payload["truncated"] is True
    kinds = {a["kind"] for a in payload["advisories"]}
    assert "truncated" in kinds
    assert len(payload["callers"]) <= 5


def test_envelope_fields():
    """make_envelope adds schema_version and project."""
    payload = build_impact_payload(symbol="x", conn=None)
    assert "schema_version" in payload
    assert payload["schema_version"] == "1.0"
    assert "project" in payload


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def _load_schema() -> dict:
    schema_path = Path(__file__).resolve().parents[2] / "schemas" / "impact.json"
    return json.loads(schema_path.read_text())


def test_schema_validates_basic_payload():
    conn = _FakeConn(
        symbols=[("uid-f", "func", "src/f.py", 1, "Function")],
        callers=[("uid-g", "g", "src/g.py", 2, "Function")],
        callees=[("uid-h", "h", "src/h.py", 3, "Function")],
        imports=[("os",)],
    )
    payload = build_impact_payload(symbol="func", conn=conn)
    jsonschema.validate(payload, _load_schema())


def test_schema_validates_empty_payload():
    payload = build_impact_payload(symbol="missing", conn=None)
    jsonschema.validate(payload, _load_schema())


def test_schema_validates_truncated_payload():
    conn = _FakeConn(
        symbols=[("uid-a", "a", "src/a.py", 1, "Function")],
        callers=[(f"uid-c{i}", f"c{i}", f"src/c{i}.py", i, "Function") for i in range(20)],
    )
    payload = build_impact_payload(symbol="a", conn=conn, max_nodes=3, depth=1)
    jsonschema.validate(payload, _load_schema())


# ---------------------------------------------------------------------------
# CLI wiring (deferred imports to avoid neo4j dependency)
# ---------------------------------------------------------------------------


def test_cli_basic():
    import typer
    from codegraphcontext_ext.cli import register_extensions

    app = typer.Typer()
    register_extensions(app)

    mock_target = type("T", (), {"slug": None})()
    with patch("codegraphcontext_ext.commands.impact.get_kuzu_connection") as mock_conn, \
         patch("codegraphcontext_ext.commands.impact.activate_project", return_value=mock_target):
        mock_conn.return_value = _FakeConn(
            symbols=[("uid-f", "func", "src/f.py", 1, "Function")],
        )
        result = runner.invoke(app, ["impact", "--symbol", "func"])
    assert result.exit_code == 0
    payload = _extract_json(result.output)
    assert payload["ok"] is True
    assert payload["kind"] == "impact"
    assert payload["symbol"] == "func"


def test_cli_with_kind_filter():
    import typer
    from codegraphcontext_ext.cli import register_extensions

    app = typer.Typer()
    register_extensions(app)

    mock_target = type("T", (), {"slug": None})()
    with patch("codegraphcontext_ext.commands.impact.get_kuzu_connection") as mock_conn, \
         patch("codegraphcontext_ext.commands.impact.activate_project", return_value=mock_target):
        mock_conn.return_value = _FakeConn(
            symbols=[("uid-c", "MyClass", "src/m.py", 1, "Class")],
        )
        result = runner.invoke(app, ["impact", "--symbol", "MyClass", "--kind", "Class"])
    assert result.exit_code == 0
    payload = _extract_json(result.output)
    assert payload["kind_filter"] == "Class"


def test_cli_db_offline():
    import typer
    from codegraphcontext_ext.cli import register_extensions

    app = typer.Typer()
    register_extensions(app)

    mock_target = type("T", (), {"slug": None})()
    with patch("codegraphcontext_ext.commands.impact.get_kuzu_connection", side_effect=Exception("offline")), \
         patch("codegraphcontext_ext.commands.impact.activate_project", return_value=mock_target):
        result = runner.invoke(app, ["impact", "--symbol", "foo"])
    assert result.exit_code == 0
    payload = _extract_json(result.output)
    kinds = {a["kind"] for a in payload["advisories"]}
    assert "no_graph" in kinds


def test_impact_project_slug_in_envelope():
    """Regression: build_impact_payload must thread the project slug
    into the envelope so --project routing is visible to consumers."""
    payload = build_impact_payload(symbol="foo", conn=None, project="flask")
    assert payload["project"] == "flask"


def test_impact_project_slug_default_none():
    """When no project is given, project should still appear as None."""
    payload = build_impact_payload(symbol="foo", conn=None)
    assert payload["project"] is None


def test_impact_cli_threads_project_slug():
    """The CLI command must capture activate_project().slug and pass it
    to build_impact_payload so the envelope carries the project slug."""
    import typer
    from unittest.mock import MagicMock
    from codegraphcontext_ext.cli import register_extensions

    app = typer.Typer()
    register_extensions(app)

    mock_target = MagicMock()
    mock_target.slug = "my-project"

    with patch("codegraphcontext_ext.commands.impact.activate_project", return_value=mock_target), \
         patch("codegraphcontext_ext.commands.impact.get_kuzu_connection", side_effect=Exception("offline")):
        result = runner.invoke(app, ["impact", "--symbol", "foo", "--project", "my-project"])

    assert result.exit_code == 0
    payload = _extract_json(result.output)
    assert payload["project"] == "my-project"
