"""Tests for the kkg execution-flow command.

Covers: symbol lookup, forward call-tree BFS, depth distribution,
truncation, advisories, schema validation, and CLI wiring.
"""

import json
from pathlib import Path
from unittest.mock import patch

import jsonschema
from typer.testing import CliRunner

from codegraphcontext_ext.commands.execution_flow import (
    COMMAND_NAME,
    SCHEMA_FILE,
    SUMMARY,
    _DEFAULT_DEPTH,
    _DEFAULT_MAX_NODES,
    build_execution_flow_payload,
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
    """Mock KùzuDB connection for execution-flow tests.

    Symbol lookups return self._symbols.
    Callee queries return self._callees, keyed by hop depth.
    """

    def __init__(self, symbols=None, callees_by_hop=None):
        self._symbols = symbols or []
        self._callees_by_hop = callees_by_hop or {}
        self._hop = 0

    def execute(self, query, *, parameters=None):
        q = query.lower()
        # Symbol lookup
        if parameters and "name" in parameters:
            return _FakeResult(self._symbols)
        # Callee query — returns different results per hop
        if "match (source)" in q and "calls" in q:
            rows = self._callees_by_hop.get(self._hop, [])
            self._hop += 1
            return _FakeResult(rows)
        return _FakeResult([])


# ---------------------------------------------------------------------------
# Module metadata
# ---------------------------------------------------------------------------


def test_command_metadata():
    assert COMMAND_NAME == "execution-flow"
    assert SCHEMA_FILE == "execution-flow.json"
    assert isinstance(SUMMARY, str) and len(SUMMARY) > 0


def test_defaults():
    assert _DEFAULT_DEPTH == 4
    assert _DEFAULT_MAX_NODES == 100


# ---------------------------------------------------------------------------
# Payload builder — no DB
# ---------------------------------------------------------------------------


def test_no_conn_returns_advisory():
    payload = build_execution_flow_payload(symbol="foo", conn=None)
    assert payload["ok"] is True
    assert payload["kind"] == "execution_flow"
    assert payload["symbol"] == "foo"
    assert payload["roots"] == []
    assert payload["nodes"] == []
    assert payload["edges"] == []
    kinds = {a["kind"] for a in payload["advisories"]}
    assert "no_graph" in kinds


def test_symbol_not_found():
    conn = _FakeConn(symbols=[])
    payload = build_execution_flow_payload(symbol="missing", conn=conn)
    kinds = {a["kind"] for a in payload["advisories"]}
    assert "symbol_not_found" in kinds


# ---------------------------------------------------------------------------
# Payload builder — with data
# ---------------------------------------------------------------------------


def test_basic_call_tree():
    """Root → callee_a, callee_b (1 hop)."""
    conn = _FakeConn(
        symbols=[("uid-root", "handle_request", "src/api.py", 10, "Function")],
        callees_by_hop={
            0: [
                # (caller_uid, callee_uid, name, path, line, kind)
                ("uid-root", "uid-a", "validate", "src/val.py", 5, "Function"),
                ("uid-root", "uid-b", "persist", "src/db.py", 20, "Function"),
            ],
        },
    )
    payload = build_execution_flow_payload(symbol="handle_request", conn=conn, depth=1)

    assert payload["ok"] is True
    assert len(payload["roots"]) == 1
    # nodes: root + 2 callees = 3
    assert payload["summary"]["total_nodes"] == 3
    assert payload["summary"]["total_edges"] == 2
    assert payload["summary"]["max_depth_reached"] == 1

    # Check depth distribution
    dist = payload["summary"]["depth_distribution"]
    assert dist.get(0, dist.get("0")) == 1  # root
    assert dist.get(1, dist.get("1")) == 2  # callees


def test_multi_hop_tree():
    """Root → A (hop 1) → B (hop 2)."""
    conn = _FakeConn(
        symbols=[("uid-root", "main", "src/main.py", 1, "Function")],
        callees_by_hop={
            0: [("uid-root", "uid-a", "step1", "src/s1.py", 10, "Function")],
            1: [("uid-a", "uid-b", "step2", "src/s2.py", 20, "Function")],
        },
    )
    payload = build_execution_flow_payload(symbol="main", conn=conn, depth=3)

    assert payload["summary"]["total_nodes"] == 3  # root + A + B
    assert payload["summary"]["total_edges"] == 2
    assert payload["summary"]["max_depth_reached"] == 2


def test_truncation():
    """Max nodes cap triggers truncation advisory."""
    conn = _FakeConn(
        symbols=[("uid-root", "big", "src/big.py", 1, "Function")],
        callees_by_hop={
            0: [
                ("uid-root", f"uid-{i}", f"fn{i}", f"src/f{i}.py", i, "Function")
                for i in range(200)
            ],
        },
    )
    payload = build_execution_flow_payload(
        symbol="big", conn=conn, max_nodes=5, depth=1,
    )
    assert payload["truncated"] is True
    kinds = {a["kind"] for a in payload["advisories"]}
    assert "truncated" in kinds
    assert payload["summary"]["total_nodes"] <= 5


def test_kind_filter():
    conn = _FakeConn(symbols=[("uid-cls", "Svc", "src/svc.py", 1, "Class")])
    payload = build_execution_flow_payload(symbol="Svc", kind="Class", conn=conn)
    assert payload["kind_filter"] == "Class"


def test_envelope_fields():
    payload = build_execution_flow_payload(symbol="x", conn=None)
    assert payload["schema_version"] == "1.0"
    assert "project" in payload


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def _load_schema() -> dict:
    schema_path = Path(__file__).resolve().parents[2] / "schemas" / "execution-flow.json"
    return json.loads(schema_path.read_text())


def test_schema_validates_basic():
    conn = _FakeConn(
        symbols=[("uid-r", "fn", "src/f.py", 1, "Function")],
        callees_by_hop={
            0: [("uid-r", "uid-a", "a", "src/a.py", 2, "Function")],
        },
    )
    payload = build_execution_flow_payload(symbol="fn", conn=conn, depth=1)
    jsonschema.validate(payload, _load_schema())


def test_schema_validates_empty():
    payload = build_execution_flow_payload(symbol="missing", conn=None)
    jsonschema.validate(payload, _load_schema())


def test_schema_validates_truncated():
    conn = _FakeConn(
        symbols=[("uid-r", "fn", "src/f.py", 1, "Function")],
        callees_by_hop={
            0: [(f"uid-r", f"uid-{i}", f"f{i}", f"src/{i}.py", i, "Function") for i in range(50)],
        },
    )
    payload = build_execution_flow_payload(symbol="fn", conn=conn, max_nodes=3, depth=1)
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
    with patch("codegraphcontext_ext.commands.execution_flow.get_kuzu_connection") as mock_conn, \
         patch("codegraphcontext_ext.commands.execution_flow.activate_project", return_value=mock_target):
        mock_conn.return_value = _FakeConn(
            symbols=[("uid-f", "func", "src/f.py", 1, "Function")],
        )
        result = runner.invoke(app, ["execution-flow", "--symbol", "func"])
    assert result.exit_code == 0
    payload = _extract_json(result.output)
    assert payload["ok"] is True
    assert payload["kind"] == "execution_flow"


def test_cli_with_options():
    import typer
    from codegraphcontext_ext.cli import register_extensions

    app = typer.Typer()
    register_extensions(app)

    mock_target = type("T", (), {"slug": None})()
    with patch("codegraphcontext_ext.commands.execution_flow.get_kuzu_connection") as mock_conn, \
         patch("codegraphcontext_ext.commands.execution_flow.activate_project", return_value=mock_target):
        mock_conn.return_value = _FakeConn(symbols=[])
        result = runner.invoke(app, [
            "execution-flow", "--symbol", "x", "--depth", "2", "--max-nodes", "10",
        ])
    assert result.exit_code == 0


def test_cli_db_offline():
    import typer
    from codegraphcontext_ext.cli import register_extensions

    app = typer.Typer()
    register_extensions(app)

    mock_target = type("T", (), {"slug": None})()
    with patch("codegraphcontext_ext.commands.execution_flow.get_kuzu_connection", side_effect=Exception("offline")), \
         patch("codegraphcontext_ext.commands.execution_flow.activate_project", return_value=mock_target):
        result = runner.invoke(app, ["execution-flow", "--symbol", "foo"])
    assert result.exit_code == 0
    payload = _extract_json(result.output)
    kinds = {a["kind"] for a in payload["advisories"]}
    assert "no_graph" in kinds


def test_execution_flow_project_slug_in_envelope():
    """Regression: build_execution_flow_payload must thread the project slug
    into the envelope so --project routing is visible to consumers."""
    payload = build_execution_flow_payload(symbol="foo", conn=None, project="flask")
    assert payload["project"] == "flask"


def test_execution_flow_project_slug_default_none():
    """When no project is given, project should still appear as None."""
    payload = build_execution_flow_payload(symbol="foo", conn=None)
    assert payload["project"] is None


def test_execution_flow_cli_threads_project_slug():
    """The CLI command must capture activate_project().slug and pass it
    to build_execution_flow_payload so the envelope carries the project slug."""
    import typer
    from unittest.mock import MagicMock
    from codegraphcontext_ext.cli import register_extensions

    app = typer.Typer()
    register_extensions(app)

    mock_target = MagicMock()
    mock_target.slug = "my-project"

    with patch("codegraphcontext_ext.commands.execution_flow.activate_project", return_value=mock_target), \
         patch("codegraphcontext_ext.commands.execution_flow.get_kuzu_connection", side_effect=Exception("offline")):
        result = runner.invoke(app, ["execution-flow", "--symbol", "foo", "--project", "my-project"])

    assert result.exit_code == 0
    payload = _extract_json(result.output)
    assert payload["project"] == "my-project"
