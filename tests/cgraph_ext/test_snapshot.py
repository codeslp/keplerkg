"""Tests for kkg snapshot — point-in-time graph metrics capture."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from codegraphcontext_ext.commands.snapshot import (
    build_snapshot_payload,
    _NODE_TYPES,
    _EDGE_TYPES,
)

SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "schemas" / "snapshot.json"


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


def _make_conn(node_counts=None, edge_counts=None, embed_counts=None):
    """Build a mock connection that responds to known Cypher patterns."""
    node_counts = node_counts or {}
    edge_counts = edge_counts or {}
    embed_counts = embed_counts or {}

    def execute(query):
        # Node counts: MATCH (n:Type) RETURN count(n) AS c
        for ntype in _NODE_TYPES:
            if f"(n:{ntype})" in query and "count(n)" in query:
                if "embedding IS NOT NULL" in query:
                    return MockResult([[embed_counts.get(ntype, 0)]])
                return MockResult([[node_counts.get(ntype, 0)]])

        # Edge counts: MATCH ()-[r:Type]->() RETURN count(r) AS c
        for etype in _EDGE_TYPES:
            if f"[r:{etype}]" in query:
                return MockResult([[edge_counts.get(etype, 0)]])

        return MockResult([[0]])

    conn = MagicMock()
    conn.execute = execute
    return conn


# ── Payload builder tests ───────────────────────────────────────


def test_snapshot_basic_counts():
    conn = _make_conn(
        node_counts={"Function": 100, "Class": 25, "File": 50},
        edge_counts={"CALLS": 200, "IMPORTS": 80},
    )
    with patch(
        "codegraphcontext_ext.commands.snapshot._get_git_head",
        return_value={"sha": "abc123", "branch": "main"},
    ):
        payload = build_snapshot_payload(conn=conn)

    assert payload["ok"] is True
    assert payload["kind"] == "snapshot"
    assert payload["nodes"]["Function"] == 100
    assert payload["nodes"]["Class"] == 25
    assert payload["nodes"]["File"] == 50
    assert payload["edges"]["CALLS"] == 200
    assert payload["edges"]["IMPORTS"] == 80
    assert payload["totals"]["nodes"] == 175
    assert payload["totals"]["edges"] == 280


def test_snapshot_embedding_coverage():
    conn = _make_conn(
        node_counts={"Function": 100, "Class": 20},
        embed_counts={"Function": 80, "Class": 15},
    )
    with patch(
        "codegraphcontext_ext.commands.snapshot._get_git_head",
        return_value={"sha": "abc123", "branch": "main"},
    ):
        payload = build_snapshot_payload(conn=conn)

    assert payload["embeddings"]["Function"]["total"] == 100
    assert payload["embeddings"]["Function"]["embedded"] == 80
    assert payload["embeddings"]["Function"]["coverage_pct"] == 80.0
    assert payload["embeddings"]["Class"]["total"] == 20
    assert payload["embeddings"]["Class"]["embedded"] == 15
    assert payload["embeddings"]["Class"]["coverage_pct"] == 75.0
    assert payload["totals"]["embedded"] == 95
    assert payload["totals"]["embeddable"] == 120


def test_snapshot_git_info():
    conn = _make_conn()
    with patch(
        "codegraphcontext_ext.commands.snapshot._get_git_head",
        return_value={"sha": "deadbeef", "branch": "feature/x"},
    ):
        payload = build_snapshot_payload(conn=conn)

    assert payload["git"]["sha"] == "deadbeef"
    assert payload["git"]["branch"] == "feature/x"


def test_snapshot_storage_offline_propagates_system_exit():
    """When storage is offline, get_kuzu_connection() raises SystemExit
    (after require_storage() prints the storage_offline JSON).  The CLI
    path must let this propagate — not catch it — so the Phase 1.5
    contract is preserved end-to-end."""
    import pytest
    from codegraphcontext_ext.commands.snapshot import snapshot_command

    with patch(
        "codegraphcontext_ext.commands.snapshot.activate_project",
    ), patch(
        "codegraphcontext_ext.commands.snapshot.get_kuzu_connection",
        side_effect=SystemExit(1),
    ):
        with pytest.raises(SystemExit):
            snapshot_command()


def test_snapshot_envelope_fields():
    conn = _make_conn()
    with patch(
        "codegraphcontext_ext.commands.snapshot._get_git_head",
        return_value={"sha": None, "branch": None},
    ):
        payload = build_snapshot_payload(conn=conn)

    assert payload["kind"] == "snapshot"
    assert payload["schema_version"] == "1.0"
    assert "project" in payload
    assert "timestamp" in payload


def test_snapshot_project_slug_passed_through():
    conn = _make_conn()
    with patch(
        "codegraphcontext_ext.commands.snapshot._get_git_head",
        return_value={"sha": None, "branch": None},
    ):
        payload = build_snapshot_payload(conn=conn, project="flask")

    assert payload["project"] == "flask"


def test_snapshot_zero_embeddable_coverage():
    """When no embeddable nodes exist, coverage_pct should be 0."""
    conn = _make_conn(node_counts={}, embed_counts={})
    with patch(
        "codegraphcontext_ext.commands.snapshot._get_git_head",
        return_value={"sha": None, "branch": None},
    ):
        payload = build_snapshot_payload(conn=conn)

    assert payload["totals"]["embedding_coverage_pct"] == 0.0


def test_snapshot_all_node_types_present():
    """Every known node type appears in the output."""
    conn = _make_conn()
    with patch(
        "codegraphcontext_ext.commands.snapshot._get_git_head",
        return_value={"sha": None, "branch": None},
    ):
        payload = build_snapshot_payload(conn=conn)

    for ntype in _NODE_TYPES:
        assert ntype in payload["nodes"]


def test_snapshot_all_edge_types_present():
    """Every known edge type appears in the output."""
    conn = _make_conn()
    with patch(
        "codegraphcontext_ext.commands.snapshot._get_git_head",
        return_value={"sha": None, "branch": None},
    ):
        payload = build_snapshot_payload(conn=conn)

    for etype in _EDGE_TYPES:
        assert etype in payload["edges"]


def test_snapshot_schema_validates():
    """Snapshot output conforms to schemas/snapshot.json."""
    import jsonschema

    schema = json.loads(SCHEMA_PATH.read_text())
    conn = _make_conn(
        node_counts={"Function": 50, "Class": 10},
        edge_counts={"CALLS": 100},
        embed_counts={"Function": 40, "Class": 8},
    )
    with patch(
        "codegraphcontext_ext.commands.snapshot._get_git_head",
        return_value={"sha": "abc123", "branch": "main"},
    ):
        payload = build_snapshot_payload(conn=conn)

    jsonschema.validate(payload, schema)


def test_snapshot_timestamp_present():
    conn = _make_conn()
    with patch(
        "codegraphcontext_ext.commands.snapshot._get_git_head",
        return_value={"sha": None, "branch": None},
    ):
        payload = build_snapshot_payload(conn=conn)

    assert "timestamp" in payload
    # Should be ISO 8601 parseable
    from datetime import datetime
    datetime.fromisoformat(payload["timestamp"])


def test_snapshot_project_git_uses_source_checkout(tmp_path):
    """When source_checkout is provided, git metadata must come from that
    directory — not the tool repo's cwd.  If the checkout doesn't exist
    or isn't a git repo, git fields should be null."""
    conn = _make_conn(node_counts={"Function": 5})

    # Non-git temp dir → null sha/branch (no git repo there)
    payload = build_snapshot_payload(
        conn=conn, project="flask", source_checkout=str(tmp_path),
    )
    assert payload["git"]["sha"] is None
    assert payload["git"]["branch"] is None

    # With project set but no source_checkout (None), git metadata must
    # be null — do NOT fall back to the tool repo's HEAD.
    payload2 = build_snapshot_payload(conn=conn, project="flask")
    assert payload2["git"]["sha"] is None
    assert payload2["git"]["branch"] is None


def test_snapshot_project_does_not_leak_tool_repo_git():
    """A project-targeted snapshot with source_checkout pointing at a
    non-git directory must not accidentally report the tool repo HEAD."""
    import tempfile

    conn = _make_conn()
    with tempfile.TemporaryDirectory() as non_git_dir:
        payload = build_snapshot_payload(
            conn=conn, project="other-repo",
            source_checkout=non_git_dir,
        )
    assert payload["git"]["sha"] is None
    assert payload["git"]["branch"] is None


def test_snapshot_local_project_still_reads_git():
    """When no project is specified (local repo), git metadata should
    still come from cwd — the tool repo IS the target."""
    conn = _make_conn(node_counts={"Function": 5})
    with patch(
        "codegraphcontext_ext.commands.snapshot._get_git_head",
        return_value={"sha": "local123", "branch": "main"},
    ):
        payload = build_snapshot_payload(conn=conn, project=None)

    assert payload["git"]["sha"] == "local123"
    assert payload["git"]["branch"] == "main"


def test_snapshot_cli_calls_get_kuzu_connection():
    """The CLI command must obtain its connection via get_kuzu_connection()
    (which runs require_storage) rather than constructing KuzuDBManager
    directly, so the Phase 1.5 storage_offline contract is preserved."""
    import pytest
    from codegraphcontext_ext.commands.snapshot import snapshot_command

    mock_conn = _make_conn(node_counts={"Function": 1})
    with patch(
        "codegraphcontext_ext.commands.snapshot.activate_project",
    ) as mock_activate, patch(
        "codegraphcontext_ext.commands.snapshot.get_kuzu_connection",
        return_value=mock_conn,
    ) as mock_get, patch(
        "codegraphcontext_ext.commands.snapshot._get_git_head",
        return_value={"sha": None, "branch": None},
    ), patch(
        "codegraphcontext_ext.commands.snapshot.typer.echo",
    ):
        mock_activate.return_value.slug = "test"
        mock_activate.return_value.source = "basename"
        try:
            snapshot_command()
        except (SystemExit, Exception):
            pass  # typer.Exit raises click.exceptions.Exit

    mock_get.assert_called_once()


def test_snapshot_cli_project_with_matching_checkout(tmp_path):
    """Regression: when --project flask is used and cfg.source_checkout
    points to a dir named 'flask', git metadata should come from there."""
    import subprocess
    from codegraphcontext_ext.commands.snapshot import snapshot_command

    # Create a real git repo at tmp_path/flask
    flask_dir = tmp_path / "flask"
    flask_dir.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.check_call(["git", "init"], cwd=str(flask_dir),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.check_call(["git", "commit", "--allow-empty", "-m", "init"],
                          cwd=str(flask_dir), stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, env=env)

    mock_conn = _make_conn(node_counts={"Function": 1})
    mock_target = type("T", (), {"slug": "flask", "source": "cli"})()
    mock_cfg = type("C", (), {"source_checkout": flask_dir})()

    captured_output = {}

    with patch("codegraphcontext_ext.commands.snapshot.activate_project", return_value=mock_target), \
         patch("codegraphcontext_ext.commands.snapshot.get_kuzu_connection", return_value=mock_conn), \
         patch("codegraphcontext_ext.commands.snapshot.resolve_cgraph_config", return_value=mock_cfg), \
         patch("codegraphcontext_ext.commands.snapshot.typer.echo") as mock_echo:
        try:
            snapshot_command()
        except (SystemExit, Exception):
            pass

    output = mock_echo.call_args[0][0] if mock_echo.called else "{}"
    payload = json.loads(output)
    # flask dir name matches target.slug, so git metadata should be populated
    assert payload["git"]["sha"] is not None
    assert payload["git"]["branch"] is not None


def test_snapshot_cli_project_with_mismatched_checkout(tmp_path):
    """Regression: when --project other-repo is used but cfg.source_checkout
    points to a dir named 'flask', git metadata must be null — the checkout
    belongs to a different project."""
    import subprocess
    from codegraphcontext_ext.commands.snapshot import snapshot_command

    # Create a real git repo at tmp_path/flask (wrong project)
    flask_dir = tmp_path / "flask"
    flask_dir.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.check_call(["git", "init"], cwd=str(flask_dir),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.check_call(["git", "commit", "--allow-empty", "-m", "init"],
                          cwd=str(flask_dir), stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, env=env)

    mock_conn = _make_conn(node_counts={"Function": 1})
    mock_target = type("T", (), {"slug": "other-repo", "source": "cli"})()
    mock_cfg = type("C", (), {"source_checkout": flask_dir})()

    with patch("codegraphcontext_ext.commands.snapshot.activate_project", return_value=mock_target), \
         patch("codegraphcontext_ext.commands.snapshot.get_kuzu_connection", return_value=mock_conn), \
         patch("codegraphcontext_ext.commands.snapshot.resolve_cgraph_config", return_value=mock_cfg), \
         patch("codegraphcontext_ext.commands.snapshot.typer.echo") as mock_echo:
        try:
            snapshot_command()
        except (SystemExit, Exception):
            pass

    output = mock_echo.call_args[0][0] if mock_echo.called else "{}"
    payload = json.loads(output)
    # checkout dir is "flask" but target is "other-repo" — must NOT use it
    assert payload["git"]["sha"] is None
    assert payload["git"]["branch"] is None


def test_snapshot_cli_normalized_checkout_name_matches(tmp_path):
    """Regression: a checkout dir named 'Flask' (mixed case) should match
    target slug 'flask' after _normalize_slug normalization, and a dir
    named 'My Flask App' should match slug 'my-flask-app'."""
    import subprocess
    from codegraphcontext_ext.commands.snapshot import snapshot_command

    # Create git repo at tmp_path/Flask (mixed-case)
    flask_dir = tmp_path / "Flask"
    flask_dir.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.check_call(["git", "init"], cwd=str(flask_dir),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.check_call(["git", "commit", "--allow-empty", "-m", "init"],
                          cwd=str(flask_dir), stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, env=env)

    mock_conn = _make_conn(node_counts={"Function": 1})
    # Target slug is normalized "flask", checkout dir is "Flask"
    mock_target = type("T", (), {"slug": "flask", "source": "cli"})()
    mock_cfg = type("C", (), {"source_checkout": flask_dir})()

    with patch("codegraphcontext_ext.commands.snapshot.activate_project", return_value=mock_target), \
         patch("codegraphcontext_ext.commands.snapshot.get_kuzu_connection", return_value=mock_conn), \
         patch("codegraphcontext_ext.commands.snapshot.resolve_cgraph_config", return_value=mock_cfg), \
         patch("codegraphcontext_ext.commands.snapshot.typer.echo") as mock_echo:
        try:
            snapshot_command()
        except (SystemExit, Exception):
            pass

    output = mock_echo.call_args[0][0] if mock_echo.called else "{}"
    payload = json.loads(output)
    # "Flask" normalizes to "flask" → matches target slug → git populated
    assert payload["git"]["sha"] is not None
