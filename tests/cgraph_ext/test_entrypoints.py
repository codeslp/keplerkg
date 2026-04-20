"""Tests for the Phase 5.7 entrypoints command."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import jsonschema
import typer
from typer.testing import CliRunner

from codegraphcontext_ext.commands.entrypoints import (
    COMMAND_NAME,
    SCHEMA_FILE,
    SUMMARY,
    _DEFAULT_LIMIT,
    build_entrypoints_payload,
    entrypoints_command,
)
from codegraphcontext_ext.project import ProjectTarget

runner = CliRunner()


class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)
        self._index = 0

    def has_next(self):
        return self._index < len(self._rows)

    def get_next(self):
        row = self._rows[self._index]
        self._index += 1
        return row


class _FakeConn:
    def __init__(self, *, functions=None, in_degree=None):
        self._functions = list(functions or [])
        self._in_degree = dict(in_degree or {})

    def execute(self, query, *, parameters=None):
        q = query.lower()
        if "return count(caller) as in_degree" in q:
            uid = parameters["uid"] if parameters else None
            return _FakeResult([(self._in_degree.get(uid, 0),)])
        if "match (f:function)" in q:
            return _FakeResult(self._functions)
        return _FakeResult([])


def _entrypoints_app() -> typer.Typer:
    app = typer.Typer()

    @app.callback()
    def _root() -> None:
        return None

    app.command(name=COMMAND_NAME, help=SUMMARY)(entrypoints_command)
    return app


def _load_schema() -> dict:
    schema_path = Path(__file__).resolve().parents[2] / "schemas" / "entrypoints.json"
    return json.loads(schema_path.read_text())


def test_command_metadata():
    assert COMMAND_NAME == "entrypoints"
    assert SCHEMA_FILE == "entrypoints.json"
    assert isinstance(SUMMARY, str) and SUMMARY


def test_default_limit():
    assert _DEFAULT_LIMIT == 20


def test_build_entrypoints_payload_ranks_by_decorator_and_in_degree():
    conn = _FakeConn(
        functions=[
            (
                "uid-route",
                "login",
                "src/auth.py",
                10,
                ["@app.route('/login')"],
            ),
            (
                "uid-cli",
                "sync_data",
                "src/cli.py",
                4,
                ["@app.command()"],
            ),
            (
                "uid-fixture",
                "db_session",
                "tests/conftest.py",
                12,
                ["@pytest.fixture(scope='session')"],
            ),
            (
                "uid-ignore",
                "helper",
                "src/helpers.py",
                8,
                ["@dataclass"],
            ),
        ],
        in_degree={
            "uid-route": 6,
            "uid-cli": 2,
            "uid-fixture": 0,
            "uid-ignore": 50,
        },
    )

    payload = build_entrypoints_payload(conn=conn, project="demo-project")

    assert payload["ok"] is True
    assert payload["kind"] == "entrypoints"
    assert payload["project"] == "demo-project"
    assert [item["uid"] for item in payload["entrypoints"]] == [
        "uid-route",
        "uid-cli",
        "uid-fixture",
    ]
    assert payload["entrypoints"][0]["framework"] == "flask"
    assert payload["entrypoints"][0]["decorators"] == ["app.route"]
    assert payload["entrypoints"][0]["in_degree"] == 6
    assert payload["entrypoints"][0]["score"] == 6.5
    assert payload["summary"] == {
        "total": 3,
        "limit": 20,
        "frameworks_detected": ["click", "flask", "pytest"],
    }
    assert payload["advisories"] == []


def test_framework_filter_limits_results():
    conn = _FakeConn(
        functions=[
            ("uid-fastapi", "list_users", "src/api.py", 9, ["@router.get('/users')"]),
            ("uid-cli", "sync_data", "src/cli.py", 4, ["@app.command()"]),
        ],
        in_degree={
            "uid-fastapi": 3,
            "uid-cli": 1,
        },
    )

    payload = build_entrypoints_payload(
        conn=conn,
        framework_filter="click",
        project="demo-project",
    )

    assert payload["framework_filter"] == "click"
    assert [item["uid"] for item in payload["entrypoints"]] == ["uid-cli"]
    assert payload["summary"]["frameworks_detected"] == ["click"]


def test_truncation_advisory_when_limit_applies():
    conn = _FakeConn(
        functions=[
            ("uid-a", "a", "src/a.py", 1, ["@app.route('/a')"]),
            ("uid-b", "b", "src/b.py", 2, ["@router.get('/b')"]),
            ("uid-c", "c", "src/c.py", 3, ["@pytest.fixture"]),
        ],
        in_degree={
            "uid-a": 4,
            "uid-b": 2,
            "uid-c": 1,
        },
    )

    payload = build_entrypoints_payload(conn=conn, limit=2)

    assert len(payload["entrypoints"]) == 2
    assert payload["summary"]["total"] == 3
    assert any(advisory["kind"] == "truncated" for advisory in payload["advisories"])


def test_no_graph_returns_advisory():
    payload = build_entrypoints_payload(conn=None, project="demo-project")

    assert payload["ok"] is True
    assert payload["kind"] == "entrypoints"
    assert payload["project"] == "demo-project"
    assert payload["entrypoints"] == []
    assert payload["summary"]["total"] == 0
    assert payload["summary"]["frameworks_detected"] == []
    assert any(advisory["kind"] == "no_graph" for advisory in payload["advisories"])


def test_schema_validates_ranked_payload():
    conn = _FakeConn(
        functions=[
            ("uid-route", "login", "src/auth.py", 10, ["@app.route('/login')"]),
            ("uid-cli", "sync_data", "src/cli.py", 4, ["@app.command()"]),
        ],
        in_degree={
            "uid-route": 2,
            "uid-cli": 1,
        },
    )

    payload = build_entrypoints_payload(conn=conn, project="demo-project")
    jsonschema.validate(payload, _load_schema())


def test_schema_validates_no_graph_payload():
    payload = build_entrypoints_payload(conn=None, project="demo-project")
    jsonschema.validate(payload, _load_schema())


def test_cli_basic():
    app = _entrypoints_app()
    conn = _FakeConn(
        functions=[
            ("uid-route", "login", "src/auth.py", 10, ["@app.route('/login')"]),
            ("uid-cli", "sync_data", "src/cli.py", 4, ["@app.command()"]),
        ],
        in_degree={
            "uid-route": 2,
            "uid-cli": 1,
        },
    )
    target = ProjectTarget(
        slug="demo-project",
        db_path=Path("/tmp/demo/kuzudb"),
        source="cli",
    )

    with patch(
        "codegraphcontext_ext.commands.entrypoints.activate_project",
        return_value=target,
    ), patch(
        "codegraphcontext_ext.commands.entrypoints.get_kuzu_connection",
        return_value=conn,
    ):
        result = runner.invoke(
            app,
            [
                "entrypoints",
                "--framework",
                "flask",
                "--limit",
                "5",
                "--project",
                "demo-project",
            ],
        )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["kind"] == "entrypoints"
    assert payload["project"] == "demo-project"
    assert payload["framework_filter"] == "flask"
    assert payload["summary"]["limit"] == 5
    assert [item["uid"] for item in payload["entrypoints"]] == ["uid-route"]
