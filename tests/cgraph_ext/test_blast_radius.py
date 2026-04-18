"""Tests for the kkg blast-radius command.

Covers: graph expansion (callers/callees outside scope), cross-module
impact, lock overlap detection, truncation, advisories, schema
validation, and CLI wiring.
"""

import json
from unittest.mock import patch

import jsonschema
import typer
from typer.testing import CliRunner

from codegraphcontext_ext.cli import register_extensions
from codegraphcontext_ext.commands.blast_radius import (
    COMMAND_NAME,
    SCHEMA_FILE,
    SUMMARY,
    _DEFAULT_MAX_NODES,
    _detect_lock_overlaps,
    build_blast_radius_payload,
)

runner = CliRunner()


def _extract_json(output: str) -> dict:
    """Extract the JSON object from CLI output (may have stderr mixed in)."""
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise ValueError(f"No JSON found in output: {output!r}")


def _blast_app() -> typer.Typer:
    app = typer.Typer()

    @app.callback()
    def _root() -> None:
        return None

    register_extensions(app)
    return app


# ---------------------------------------------------------------------------
# Module-level metadata
# ---------------------------------------------------------------------------


def test_command_metadata():
    assert COMMAND_NAME == "blast-radius"
    assert SCHEMA_FILE == "blast-radius.json"
    assert isinstance(SUMMARY, str)
    assert len(SUMMARY) > 0


def test_default_max_nodes():
    assert _DEFAULT_MAX_NODES == 50


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
    """Mock KùzuDB connection with canned query results."""

    def __init__(self, nodes=None, callers=None, callees=None, imports=None,
                 degrees=None):
        self._nodes = nodes or []
        self._callers = callers or []
        self._callees = callees or []
        self._imports = imports or []
        self._degrees = degrees or []

    def execute(self, query, *, parameters=None):
        q = query.lower()
        if "match (caller)" in q and "calls" in q and "count" in q:
            return _FakeResult(self._degrees)
        if "match (caller)" in q and "calls" in q:
            return _FakeResult(self._callers)
        if "match (source)" in q and "calls" in q:
            return _FakeResult(self._callees)
        if "imports" in q:
            return _FakeResult(self._imports)
        # Default: node lookup
        return _FakeResult(self._nodes)


# ---------------------------------------------------------------------------
# build_blast_radius_payload tests
# ---------------------------------------------------------------------------


def test_basic_expansion():
    """Payload includes nodes in scope, callers, and callees."""
    conn = _FakeConn(
        nodes=[("uid1", "verify_token", "src/auth.py", 42, "Function")],
        callers=[("uid_pay", "pay_out", "src/billing.py", 99, "Function")],
        callees=[("uid_log", "log_auth", "src/logging.py", 10, "Function")],
    )

    payload = build_blast_radius_payload(
        files=["src/auth.py"],
        conn=conn,
    )

    assert payload["ok"] is True
    assert payload["kind"] == "blast_radius"
    assert payload["files"] == ["src/auth.py"]
    assert len(payload["nodes_in_scope"]) == 1
    assert payload["nodes_in_scope"][0]["name"] == "verify_token"
    assert len(payload["transitive_callers"]) == 1
    assert payload["transitive_callers"][0]["name"] == "pay_out"
    assert len(payload["transitive_callees"]) == 1
    assert payload["transitive_callees"][0]["name"] == "log_auth"
    assert payload["summary"]["files_requested"] == 1
    assert payload["summary"]["nodes_in_scope"] == 1
    assert payload["summary"]["transitive_callers"] == 1
    assert payload["summary"]["transitive_callees"] == 1


def test_no_graph_connection():
    """Without a DB connection, payload is valid but empty + advisory."""
    payload = build_blast_radius_payload(
        files=["src/auth.py"],
        conn=None,
    )

    assert payload["ok"] is True
    assert payload["nodes_in_scope"] == []
    assert payload["transitive_callers"] == []
    assert payload["transitive_callees"] == []
    kinds = {a["kind"] for a in payload["advisories"]}
    assert "no_graph" in kinds


def test_cross_module_impact():
    """Cross-module imports are captured in the payload."""
    conn = _FakeConn(
        imports=[("os.path",), ("json",), ("os.environ",)],
    )

    payload = build_blast_radius_payload(
        files=["src/auth.py"],
        conn=conn,
    )

    # os.path and os.environ collapse to "os"; json stays
    assert "json" in payload["cross_module_impact"]
    assert "os" in payload["cross_module_impact"]


def test_empty_files():
    """No files produces valid empty payload."""
    payload = build_blast_radius_payload(
        files=[],
        conn=_FakeConn(),
    )

    assert payload["ok"] is True
    assert payload["nodes_in_scope"] == []
    assert payload["summary"]["files_requested"] == 0


def test_multiple_files():
    """Multiple file paths are all included."""
    conn = _FakeConn(
        nodes=[
            ("uid1", "func_a", "src/auth.py", 10, "Function"),
            ("uid2", "func_b", "src/billing.py", 20, "Function"),
        ],
    )

    payload = build_blast_radius_payload(
        files=["src/auth.py", "src/billing.py"],
        conn=conn,
    )

    assert payload["files"] == ["src/auth.py", "src/billing.py"]
    assert len(payload["nodes_in_scope"]) == 2
    assert payload["summary"]["files_requested"] == 2


# ---------------------------------------------------------------------------
# Lock overlap detection
# ---------------------------------------------------------------------------


def test_transitive_callers_multi_hop():
    """Transitive expansion discovers callers beyond the first hop."""

    class _MultiHopConn:
        """Returns different callers depending on which UIDs are in the query."""

        def execute(self, query, *, parameters=None):
            q = query.lower()
            if "match (caller)" in q and "calls" in q and "count" not in q:
                # Hop 1: uid1 is called by uid_hop1
                if "'uid1'" in q and "'uid_hop1'" not in q:
                    return _FakeResult([
                        ("uid_hop1", "hop1_func", "src/hop1.py", 10, "Function"),
                    ])
                # Hop 2: uid_hop1 is called by uid_hop2
                if "'uid_hop1'" in q and "'uid_hop2'" not in q:
                    return _FakeResult([
                        ("uid_hop2", "hop2_func", "src/hop2.py", 20, "Function"),
                    ])
                return _FakeResult([])
            if "match (source)" in q and "calls" in q:
                return _FakeResult([])
            if "imports" in q:
                return _FakeResult([])
            if "count(caller)" in q:
                return _FakeResult([])
            # Node lookup
            return _FakeResult([
                ("uid1", "verify_token", "src/auth.py", 42, "Function"),
            ])

    payload = build_blast_radius_payload(
        files=["src/auth.py"],
        conn=_MultiHopConn(),
    )

    caller_names = {c["name"] for c in payload["transitive_callers"]}
    assert "hop1_func" in caller_names, "First-hop caller should be found"
    assert "hop2_func" in caller_names, "Second-hop caller should be found"
    assert payload["summary"]["transitive_callers"] == 2


def test_transitive_callees_multi_hop():
    """Transitive expansion discovers callees beyond the first hop."""

    class _MultiHopConn:
        def execute(self, query, *, parameters=None):
            q = query.lower()
            if "match (source)" in q and "calls" in q:
                if "'uid1'" in q and "'uid_hop1'" not in q:
                    return _FakeResult([
                        ("uid_hop1", "hop1_callee", "src/hop1.py", 10, "Function"),
                    ])
                if "'uid_hop1'" in q and "'uid_hop2'" not in q:
                    return _FakeResult([
                        ("uid_hop2", "hop2_callee", "src/hop2.py", 20, "Function"),
                    ])
                return _FakeResult([])
            if "match (caller)" in q and "calls" in q:
                return _FakeResult([])
            if "imports" in q:
                return _FakeResult([])
            if "count(caller)" in q:
                return _FakeResult([])
            return _FakeResult([
                ("uid1", "start_func", "src/auth.py", 1, "Function"),
            ])

    payload = build_blast_radius_payload(
        files=["src/auth.py"],
        conn=_MultiHopConn(),
    )

    callee_names = {c["name"] for c in payload["transitive_callees"]}
    assert "hop1_callee" in callee_names
    assert "hop2_callee" in callee_names
    assert payload["summary"]["transitive_callees"] == 2


def test_non_dict_locks_json():
    """A valid JSON array for --locks-json should produce advisory, not crash."""
    payload = build_blast_radius_payload(
        files=["src/auth.py"],
        locks_json='["x"]',
        conn=_FakeConn(),
    )

    assert payload["ok"] is True
    kinds = {a["kind"] for a in payload["advisories"]}
    assert "invalid_locks_json" in kinds
    assert payload["lock_overlaps"] == []


def test_malformed_lock_values():
    """Non-list lock values (e.g. int) emit advisory, don't crash."""
    payload = build_blast_radius_payload(
        files=["src/auth.py"],
        locks_json='{"a": 1, "b": "not-a-list"}',
        conn=_FakeConn(
            nodes=[("uid1", "f", "src/auth.py", 1, "Function")],
            callers=[("uid2", "g", "src/billing.py", 1, "Function")],
        ),
    )

    assert payload["ok"] is True
    kinds = {a["kind"] for a in payload["advisories"]}
    assert "invalid_locks_json" in kinds
    assert payload["lock_overlaps"] == []


def test_mixed_valid_and_invalid_lock_values():
    """Valid lock entries still work when mixed with invalid ones."""
    payload = build_blast_radius_payload(
        files=["src/auth.py"],
        locks_json='{"a": 1, "c": ["src/billing.py"]}',
        conn=_FakeConn(
            nodes=[("uid1", "f", "src/auth.py", 1, "Function")],
            callers=[("uid2", "g", "src/billing.py", 1, "Function")],
        ),
    )

    assert payload["ok"] is True
    # Advisory for bad lane "a"
    kinds = {a["kind"] for a in payload["advisories"]}
    assert "invalid_locks_json" in kinds
    # Valid lane "c" still produces overlap
    assert len(payload["lock_overlaps"]) == 1
    assert payload["lock_overlaps"][0]["lane"] == "c"


def test_three_hop_callers():
    """Transitive expansion with default depth=5 discovers 3rd-hop callers."""

    class _ThreeHopConn:
        def execute(self, query, *, parameters=None):
            q = query.lower()
            if "match (caller)" in q and "calls" in q and "count" not in q:
                if "'uid1'" in q and "'uid_hop1'" not in q:
                    return _FakeResult([("uid_hop1", "h1", "src/h1.py", 1, "Function")])
                if "'uid_hop1'" in q and "'uid_hop2'" not in q:
                    return _FakeResult([("uid_hop2", "h2", "src/h2.py", 1, "Function")])
                if "'uid_hop2'" in q and "'uid_hop3'" not in q:
                    return _FakeResult([("uid_hop3", "h3", "src/h3.py", 1, "Function")])
                return _FakeResult([])
            if "match (source)" in q and "calls" in q:
                return _FakeResult([])
            if "imports" in q:
                return _FakeResult([])
            if "count(caller)" in q:
                return _FakeResult([])
            return _FakeResult([("uid1", "root", "src/auth.py", 1, "Function")])

    payload = build_blast_radius_payload(
        files=["src/auth.py"],
        conn=_ThreeHopConn(),
    )

    caller_names = {c["name"] for c in payload["transitive_callers"]}
    assert "h1" in caller_names
    assert "h2" in caller_names
    assert "h3" in caller_names, "Third-hop caller should be found with default depth=5"
    assert payload["summary"]["transitive_callers"] == 3


def test_lock_overlap_detection():
    """Overlaps with other lanes are detected and reported."""
    conn = _FakeConn(
        nodes=[("uid1", "verify_token", "src/auth.py", 42, "Function")],
        callers=[("uid_pay", "pay_out", "src/billing.py", 99, "Function")],
    )
    locks = {"c": ["src/billing.py"]}

    payload = build_blast_radius_payload(
        files=["src/auth.py"],
        lane="a",
        locks_json=json.dumps(locks),
        conn=conn,
    )

    assert len(payload["lock_overlaps"]) == 1
    assert payload["lock_overlaps"][0]["lane"] == "c"
    assert "src/billing.py" in payload["lock_overlaps"][0]["overlapping_files"]
    assert payload["summary"]["lock_overlaps"] == 1
    # Advisory produced
    overlap_advisories = [a for a in payload["advisories"] if a["kind"] == "lock_overlap"]
    assert len(overlap_advisories) == 1


def test_own_lane_excluded_from_overlap():
    """Own lane is excluded from overlap detection."""
    callers = [{"uid": "u1", "name": "f", "file": "src/billing.py:10", "kind": "Function"}]
    callees = []
    locks = {"a": ["src/billing.py"], "b": ["src/other.py"]}

    overlaps = _detect_lock_overlaps(callers, callees, locks, own_lane="a")

    # Lane "a" is excluded; lane "b" doesn't overlap
    assert len(overlaps) == 0


def test_directory_lock_overlap():
    """Directory-level locks match files under that directory."""
    callers = [{"uid": "u1", "name": "f", "file": "src/auth/tokens.py:10", "kind": "Function"}]
    callees = []
    locks = {"b": ["src/auth/"]}

    overlaps = _detect_lock_overlaps(callers, callees, locks, own_lane="a")

    assert len(overlaps) == 1
    assert overlaps[0]["lane"] == "b"


def test_invalid_locks_json():
    """Invalid JSON in --locks-json produces an advisory but doesn't crash."""
    payload = build_blast_radius_payload(
        files=["src/auth.py"],
        locks_json="not-valid-json{",
        conn=_FakeConn(),
    )

    assert payload["ok"] is True
    kinds = {a["kind"] for a in payload["advisories"]}
    assert "invalid_locks_json" in kinds
    assert payload["lock_overlaps"] == []


def test_no_locks_no_overlaps():
    """Without locks, no overlaps are reported."""
    payload = build_blast_radius_payload(
        files=["src/auth.py"],
        conn=_FakeConn(
            nodes=[("uid1", "f", "src/auth.py", 1, "Function")],
            callers=[("uid2", "g", "src/billing.py", 1, "Function")],
        ),
    )

    assert payload["lock_overlaps"] == []
    assert payload["summary"]["lock_overlaps"] == 0


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


def test_truncation():
    """When results exceed max_nodes, payload is truncated with advisory."""
    nodes = [(f"uid{i}", f"func_{i}", "src/big.py", i, "Function") for i in range(10)]
    conn = _FakeConn(nodes=nodes)

    payload = build_blast_radius_payload(
        files=["src/big.py"],
        max_nodes=3,
        conn=conn,
    )

    assert len(payload["nodes_in_scope"]) == 3
    assert payload["truncated"] is True
    assert payload["total_nodes"]["in_scope"] == 10
    kinds = {a["kind"] for a in payload["advisories"]}
    assert "truncated" in kinds


def test_no_truncation_within_limit():
    """When results are within max_nodes, no truncation fields."""
    conn = _FakeConn(
        nodes=[("uid1", "f", "src/a.py", 1, "Function")],
    )

    payload = build_blast_radius_payload(
        files=["src/a.py"],
        max_nodes=50,
        conn=conn,
    )

    assert "truncated" not in payload
    assert "total_nodes" not in payload


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def test_schema_validation():
    """Payload validates against the blast-radius JSON schema."""
    import pathlib
    schema_path = pathlib.Path(__file__).resolve().parents[2] / "schemas" / "blast-radius.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    conn = _FakeConn(
        nodes=[("uid1", "verify_token", "src/auth.py", 42, "Function")],
        callers=[("uid_pay", "pay_out", "src/billing.py", 99, "Function")],
        callees=[("uid_log", "log_auth", "src/logging.py", 10, "Function")],
        imports=[("os.path",)],
    )
    locks = {"c": ["src/billing.py"]}

    payload = build_blast_radius_payload(
        files=["src/auth.py"],
        lane="a",
        locks_json=json.dumps(locks),
        conn=conn,
    )

    jsonschema.validate(payload, schema)


def test_schema_validation_empty():
    """Empty payload (no graph) validates against schema."""
    import pathlib
    schema_path = pathlib.Path(__file__).resolve().parents[2] / "schemas" / "blast-radius.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    payload = build_blast_radius_payload(files=["src/a.py"], conn=None)
    jsonschema.validate(payload, schema)


def test_schema_validation_truncated():
    """Truncated payload validates against schema."""
    import pathlib
    schema_path = pathlib.Path(__file__).resolve().parents[2] / "schemas" / "blast-radius.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    nodes = [(f"uid{i}", f"func_{i}", "src/big.py", i, "Function") for i in range(10)]
    conn = _FakeConn(nodes=nodes)

    payload = build_blast_radius_payload(
        files=["src/big.py"],
        max_nodes=3,
        conn=conn,
    )

    jsonschema.validate(payload, schema)


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


@patch("codegraphcontext_ext.commands.blast_radius.get_kuzu_connection")
def test_cli_basic(mock_conn):
    """CLI wires --files through to payload builder."""
    mock_conn.return_value = _FakeConn(
        nodes=[("uid1", "f", "src/auth.py", 1, "Function")],
    )

    app = _blast_app()
    result = runner.invoke(app, ["blast-radius", "--files", "src/auth.py"])

    assert result.exit_code == 0
    payload = _extract_json(result.output)
    assert payload["ok"] is True
    assert payload["kind"] == "blast_radius"
    assert payload["files"] == ["src/auth.py"]


@patch("codegraphcontext_ext.commands.blast_radius.get_kuzu_connection")
def test_cli_multiple_files(mock_conn):
    """CLI handles comma-separated files."""
    mock_conn.return_value = _FakeConn()

    app = _blast_app()
    result = runner.invoke(app, ["blast-radius", "--files", "src/a.py, src/b.py"])

    assert result.exit_code == 0
    payload = _extract_json(result.output)
    assert payload["files"] == ["src/a.py", "src/b.py"]


@patch("codegraphcontext_ext.commands.blast_radius.get_kuzu_connection")
def test_cli_with_lane_and_locks(mock_conn):
    """CLI passes --lane and --locks-json through."""
    mock_conn.return_value = _FakeConn(
        nodes=[("uid1", "f", "src/auth.py", 1, "Function")],
        callers=[("uid2", "g", "src/billing.py", 1, "Function")],
    )
    locks = json.dumps({"c": ["src/billing.py"]})

    app = _blast_app()
    result = runner.invoke(app, [
        "blast-radius",
        "--files", "src/auth.py",
        "--lane", "a",
        "--locks-json", locks,
    ])

    assert result.exit_code == 0
    payload = _extract_json(result.output)
    assert len(payload["lock_overlaps"]) == 1


@patch("codegraphcontext_ext.commands.blast_radius.get_kuzu_connection")
def test_cli_db_unavailable(mock_conn):
    """CLI handles DB connection failure gracefully."""
    mock_conn.side_effect = RuntimeError("DB offline")

    app = _blast_app()
    result = runner.invoke(app, ["blast-radius", "--files", "src/auth.py"])

    assert result.exit_code == 0
    payload = _extract_json(result.output)
    assert payload["ok"] is True
    kinds = {a["kind"] for a in payload["advisories"]}
    assert "no_graph" in kinds


@patch("codegraphcontext_ext.commands.blast_radius.get_kuzu_connection")
def test_cli_empty_files_error(mock_conn):
    """CLI errors on empty --files."""
    mock_conn.return_value = _FakeConn()

    app = _blast_app()
    # Typer requires --files, so passing empty string
    result = runner.invoke(app, ["blast-radius", "--files", "  "])

    assert result.exit_code == 1
    payload = _extract_json(result.output)
    assert payload["ok"] is False
    assert payload["kind"] == "no_files"
