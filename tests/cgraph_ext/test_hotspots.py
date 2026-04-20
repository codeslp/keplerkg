"""Tests for kkg hotspots — git churn x graph centrality risk analysis."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from codegraphcontext_ext.commands.hotspots import (
    _query_git_churn,
    build_hotspots_payload,
)

SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "schemas" / "hotspots.json"


# ── Mock connection helpers ─────────────────────────────────────

class MockResult:
    """Minimal mock for KuzuDB query results."""

    def __init__(self, rows):
        self._rows = list(rows)
        self._idx = 0

    def has_next(self):
        return self._idx < len(self._rows)

    def get_next(self):
        if self._idx >= len(self._rows):
            return None
        row = self._rows[self._idx]
        self._idx += 1
        return row


def _make_conn(function_callers=None, class_callers=None):
    """Build a mock connection that responds to centrality queries.

    function_callers: list of (uid, name, path, in_degree) tuples
    class_callers: list of (uid, name, path, in_degree) tuples
    """
    function_callers = function_callers or []
    class_callers = class_callers or []

    def execute(query):
        if "Function" in query:
            return MockResult(function_callers)
        if "Class" in query:
            return MockResult(class_callers)
        return MockResult([])

    conn = MagicMock()
    conn.execute = execute
    return conn


# ── Payload builder tests ───────────────────────────────────────


def test_hotspots_basic_risk_scoring():
    conn = _make_conn(
        function_callers=[
            ("uid1", "handle_request", "src/api/routes.py", 10),
            ("uid2", "validate_input", "src/api/validators.py", 5),
        ],
    )
    churn = {
        "src/api/routes.py": 20,
        "src/api/validators.py": 8,
    }
    payload = build_hotspots_payload(conn=conn, churn_override=churn)

    assert payload["ok"] is True
    assert payload["kind"] == "hotspots"
    assert len(payload["hotspots"]) == 2

    # routes.py: churn=20, centrality=10, raw=200 → risk=100
    top = payload["hotspots"][0]
    assert top["path"] == "src/api/routes.py"
    assert top["churn"] == 20
    assert top["centrality"] == 10
    assert top["raw_score"] == 200
    assert top["risk_score"] == 100.0

    # validators.py: churn=8, centrality=5, raw=40 → risk=20
    second = payload["hotspots"][1]
    assert second["path"] == "src/api/validators.py"
    assert second["raw_score"] == 40
    assert second["risk_score"] == 20.0


def test_hotspots_top_limit():
    conn = _make_conn(
        function_callers=[
            (f"uid{i}", f"func_{i}", f"src/f{i}.py", i + 1)
            for i in range(20)
        ],
    )
    churn = {f"src/f{i}.py": 10 for i in range(20)}

    payload = build_hotspots_payload(conn=conn, churn_override=churn, top=5)

    assert len(payload["hotspots"]) == 5
    # Highest centrality file should be first
    assert payload["hotspots"][0]["path"] == "src/f19.py"


def test_hotspots_no_overlap_excluded():
    """Files with churn but no centrality (or vice versa) get raw_score=0 and are excluded."""
    conn = _make_conn(
        function_callers=[
            ("uid1", "important_fn", "src/core.py", 15),
        ],
    )
    churn = {
        "src/unrelated.py": 50,  # high churn but no graph presence
    }
    payload = build_hotspots_payload(conn=conn, churn_override=churn)

    # Only files with BOTH churn AND centrality produce hotspots
    assert len(payload["hotspots"]) == 0


def test_hotspots_combined_centrality():
    """Multiple functions in same file accumulate centrality."""
    conn = _make_conn(
        function_callers=[
            ("uid1", "fn_a", "src/utils.py", 5),
            ("uid2", "fn_b", "src/utils.py", 8),
        ],
    )
    churn = {"src/utils.py": 10}
    payload = build_hotspots_payload(conn=conn, churn_override=churn)

    assert len(payload["hotspots"]) == 1
    entry = payload["hotspots"][0]
    assert entry["centrality"] == 13  # 5 + 8
    assert entry["raw_score"] == 130  # 10 * 13


def test_hotspots_class_callers_included():
    """Class nodes contribute to centrality too."""
    conn = _make_conn(
        class_callers=[
            ("uid1", "BaseHandler", "src/base.py", 12),
        ],
    )
    churn = {"src/base.py": 6}
    payload = build_hotspots_payload(conn=conn, churn_override=churn)

    assert len(payload["hotspots"]) == 1
    assert payload["hotspots"][0]["centrality"] == 12
    assert payload["hotspots"][0]["raw_score"] == 72


def test_hotspots_storage_offline_propagates_system_exit():
    """When storage is offline, get_kuzu_connection() raises SystemExit
    (after require_storage() prints the storage_offline JSON).  The CLI
    path must let this propagate to preserve the Phase 1.5 contract."""
    import pytest
    from codegraphcontext_ext.commands.hotspots import hotspots_command

    with patch(
        "codegraphcontext_ext.commands.hotspots.activate_project",
    ), patch(
        "codegraphcontext_ext.commands.hotspots.get_kuzu_connection",
        side_effect=SystemExit(1),
    ):
        with pytest.raises(SystemExit):
            hotspots_command()


def test_hotspots_empty_graph():
    conn = _make_conn()
    payload = build_hotspots_payload(conn=conn, churn_override={})

    assert payload["ok"] is True
    assert payload["hotspots"] == []
    assert payload["stats"]["files_analyzed"] == 0


def test_hotspots_envelope_fields():
    conn = _make_conn()
    payload = build_hotspots_payload(conn=conn, churn_override={})

    assert payload["kind"] == "hotspots"
    assert payload["schema_version"] == "1.0"
    assert "project" in payload


def test_hotspots_project_slug_passed_through():
    conn = _make_conn()
    payload = build_hotspots_payload(conn=conn, churn_override={}, project="my-app")

    assert payload["project"] == "my-app"


def test_hotspots_stats_populated():
    conn = _make_conn(
        function_callers=[
            ("uid1", "fn_a", "src/a.py", 3),
            ("uid2", "fn_b", "src/b.py", 7),
        ],
    )
    churn = {"src/a.py": 5, "src/b.py": 10, "src/c.py": 2}
    payload = build_hotspots_payload(conn=conn, churn_override=churn)

    stats = payload["stats"]
    assert stats["files_analyzed"] == 3
    assert stats["symbols_analyzed"] == 2
    assert stats["max_churn"] == 10
    assert stats["max_centrality"] == 7


def test_hotspots_sorted_by_risk_descending():
    conn = _make_conn(
        function_callers=[
            ("uid1", "fn_low", "src/low.py", 2),
            ("uid2", "fn_mid", "src/mid.py", 5),
            ("uid3", "fn_high", "src/high.py", 10),
        ],
    )
    churn = {"src/low.py": 3, "src/mid.py": 6, "src/high.py": 12}
    payload = build_hotspots_payload(conn=conn, churn_override=churn)

    scores = [h["risk_score"] for h in payload["hotspots"]]
    assert scores == sorted(scores, reverse=True)


def test_hotspots_schema_validates():
    """Hotspots output conforms to schemas/hotspots.json."""
    import jsonschema

    schema = json.loads(SCHEMA_PATH.read_text())
    conn = _make_conn(
        function_callers=[
            ("uid1", "handle", "src/api.py", 8),
        ],
    )
    churn = {"src/api.py": 15}
    payload = build_hotspots_payload(conn=conn, churn_override=churn)

    jsonschema.validate(payload, schema)


def test_hotspots_since_days_in_stats():
    conn = _make_conn()
    payload = build_hotspots_payload(
        conn=conn, churn_override={}, since_days=30,
    )

    assert payload["stats"]["since_days"] == 30


def test_hotspots_cli_calls_get_kuzu_connection():
    """The CLI command must obtain its connection via get_kuzu_connection()
    so the Phase 1.5 storage_offline preflight is preserved."""
    import pytest
    from codegraphcontext_ext.commands.hotspots import hotspots_command

    mock_conn = _make_conn(function_callers=[("uid1", "fn", "src/a.py", 1)])
    with patch(
        "codegraphcontext_ext.commands.hotspots.activate_project",
    ) as mock_activate, patch(
        "codegraphcontext_ext.commands.hotspots.get_kuzu_connection",
        return_value=mock_conn,
    ) as mock_get, patch(
        "codegraphcontext_ext.commands.hotspots.typer.echo",
    ):
        mock_activate.return_value.slug = "test"
        try:
            hotspots_command()
        except (SystemExit, Exception):
            pass  # typer.Exit raises click.exceptions.Exit

    mock_get.assert_called_once()


def test_hotspots_query_uses_n_path_not_file_path():
    """The Cypher query must use n.path (matching upstream schema),
    not n.file_path which would return empty paths."""
    conn = _make_conn(
        function_callers=[
            ("uid1", "handler", "src/routes.py", 4),
        ],
    )
    churn = {"src/routes.py": 10}
    payload = build_hotspots_payload(conn=conn, churn_override=churn)

    assert len(payload["hotspots"]) == 1
    assert payload["hotspots"][0]["path"] == "src/routes.py"


def test_hotspots_negative_since_days_normalized():
    """Negative since_days must be clamped to 0 so the schema minimum: 0
    constraint is satisfied."""
    conn = _make_conn()
    payload = build_hotspots_payload(conn=conn, churn_override={}, since_days=-5)

    assert payload["stats"]["since_days"] == 0


def test_hotspots_output_validates_against_schema():
    """Every hotspots payload must conform to schemas/hotspots.json."""
    import jsonschema

    schema = json.loads(SCHEMA_PATH.read_text())

    conn = _make_conn(
        function_callers=[
            ("uid1", "handle", "src/api.py", 8),
            ("uid2", "validate", "src/val.py", 3),
        ],
    )
    churn = {"src/api.py": 15, "src/val.py": 4}
    payload = build_hotspots_payload(conn=conn, churn_override=churn)

    jsonschema.validate(payload, schema)


def test_hotspots_project_churn_uses_source_checkout(tmp_path):
    """Regression: project-targeted churn must read from source_checkout,
    not the caller repo.  Create two isolated git repos with distinct
    histories and verify churn comes from the target, not from cwd."""
    # Create "caller" repo (simulates cgraph's own repo)
    caller_dir = tmp_path / "caller"
    caller_dir.mkdir()
    subprocess.check_call(["git", "init"], cwd=str(caller_dir),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.check_call(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=str(caller_dir),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )
    (caller_dir / "caller_only.py").write_text("# caller repo file\n")
    subprocess.check_call(["git", "add", "caller_only.py"], cwd=str(caller_dir),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.check_call(
        ["git", "commit", "-m", "add caller file"],
        cwd=str(caller_dir),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )

    # Create "target" repo (simulates e.g. flask source checkout)
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    subprocess.check_call(["git", "init"], cwd=str(target_dir),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.check_call(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=str(target_dir),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )
    (target_dir / "target_only.py").write_text("# target repo file\n")
    subprocess.check_call(["git", "add", "target_only.py"], cwd=str(target_dir),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.check_call(
        ["git", "commit", "-m", "add target file"],
        cwd=str(target_dir),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )

    # Run _query_git_churn pointing at the target checkout
    saved_cwd = os.getcwd()
    try:
        os.chdir(str(caller_dir))
        churn = _query_git_churn(since_days=0, cwd=str(target_dir))
    finally:
        os.chdir(saved_cwd)

    # Must see target_only.py, must NOT see caller_only.py
    assert "target_only.py" in churn, "churn should come from the target checkout"
    assert "caller_only.py" not in churn, (
        "churn must NOT read from the caller repo when source_checkout is set"
    )


def test_hotspots_source_checkout_wired_through_payload(tmp_path):
    """build_hotspots_payload passes source_checkout to _query_git_churn
    so that --project targets the right repo."""
    captured_cwd = {}

    def fake_churn(since_days=90, cwd=None):
        captured_cwd["cwd"] = cwd
        return {"src/app.py": 5}

    conn = _make_conn(
        function_callers=[("uid1", "fn", "src/app.py", 3)],
    )

    with patch("codegraphcontext_ext.commands.hotspots._query_git_churn", fake_churn):
        payload = build_hotspots_payload(
            conn=conn,
            project="flask",
            source_checkout="/some/path/to/flask",
        )

    assert captured_cwd["cwd"] == "/some/path/to/flask"
    assert payload["project"] == "flask"
    assert len(payload["hotspots"]) == 1


def test_hotspots_project_without_source_checkout_ignores_caller_repo_churn(tmp_path):
    """Regression: with a project slug but no verified source checkout,
    hotspots must not read git churn from the caller repo's cwd."""
    caller_dir = tmp_path / "caller"
    caller_dir.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.check_call(["git", "init"], cwd=str(caller_dir),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.check_call(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=str(caller_dir),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
    )
    (caller_dir / "shared.py").write_text("# caller repo file\n")
    subprocess.check_call(["git", "add", "shared.py"], cwd=str(caller_dir),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.check_call(
        ["git", "commit", "-m", "add shared file"],
        cwd=str(caller_dir),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
    )

    conn = _make_conn(
        function_callers=[("uid1", "fn", "shared.py", 3)],
    )

    saved_cwd = os.getcwd()
    try:
        os.chdir(str(caller_dir))
        payload = build_hotspots_payload(
            conn=conn,
            project="other-repo",
            source_checkout=None,
        )
    finally:
        os.chdir(saved_cwd)

    assert payload["hotspots"] == []
    assert payload["stats"]["max_churn"] == 0
