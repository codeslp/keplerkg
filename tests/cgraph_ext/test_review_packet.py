"""Tests for the kkg review-packet command.

Covers: fallback chain (diff→staged→workdir→untracked→locked_files),
graph node lookup, callers/callees not in diff, cross-module impact,
truncation (§4.4), advisories, conflicting flags, and CLI wiring.
"""

import json
from unittest.mock import MagicMock, patch

import typer
from typer.testing import CliRunner

from codegraphcontext_ext.cli import register_extensions
from codegraphcontext_ext.commands.review_packet import (
    COMMAND_NAME,
    SCHEMA_FILE,
    SUMMARY,
    _DEFAULT_MAX_NODES,
    _is_test_path,
    _parse_shortstat,
    _synthesize_nodes_from_file,
    _truncate_bucket,
    _truncation_suggestion,
    build_review_packet_payload,
)

runner = CliRunner()


def _extract_json(output: str) -> dict:
    """Extract the JSON object from CLI output (may have stderr mixed in)."""
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise ValueError(f"No JSON found in output: {output!r}")


def _review_app() -> typer.Typer:
    app = typer.Typer()

    @app.callback()
    def _root() -> None:
        return None

    register_extensions(app)
    return app


# ---------------------------------------------------------------------------
# Scaffold metadata
# ---------------------------------------------------------------------------


def test_command_metadata():
    assert COMMAND_NAME == "review-packet"
    assert SCHEMA_FILE == "review-packet.json"
    assert isinstance(SUMMARY, str) and len(SUMMARY) > 0


# ---------------------------------------------------------------------------
# _parse_shortstat
# ---------------------------------------------------------------------------


def test_parse_shortstat_full():
    output = " 4 files changed, 87 insertions(+), 12 deletions(-)"
    assert _parse_shortstat(output) == {"files": 4, "additions": 87, "deletions": 12}


def test_parse_shortstat_insertions_only():
    output = " 2 files changed, 30 insertions(+)"
    result = _parse_shortstat(output)
    assert result == {"files": 2, "additions": 30, "deletions": 0}


def test_parse_shortstat_empty():
    assert _parse_shortstat("") == {"files": 0, "additions": 0, "deletions": 0}


# ---------------------------------------------------------------------------
# _truncate_bucket
# ---------------------------------------------------------------------------


def test_truncate_bucket_no_truncation():
    nodes = [{"uid": "a"}, {"uid": "b"}]
    result, total = _truncate_bucket(nodes, 10)
    assert result == nodes
    assert total == 2


def test_truncate_bucket_with_truncation():
    nodes = [{"uid": f"n{i}"} for i in range(100)]
    result, total = _truncate_bucket(nodes, 5)
    assert len(result) == 5
    assert total == 100


def test_truncate_bucket_prefers_high_in_degree():
    nodes = [
        {"uid": "low", "name": "low_fn"},
        {"uid": "high", "name": "high_fn"},
        {"uid": "mid", "name": "mid_fn"},
    ]
    in_degrees = {"low": 1, "high": 100, "mid": 10}
    result, total = _truncate_bucket(nodes, 2, in_degrees)
    assert total == 3
    assert len(result) == 2
    assert result[0]["uid"] == "high"
    assert result[1]["uid"] == "mid"


# ---------------------------------------------------------------------------
# _truncation_suggestion
# ---------------------------------------------------------------------------


def test_truncation_suggestion_locked_files_untracked():
    suggestion = _truncation_suggestion("locked_files", [{"kind": "untracked_only"}])
    assert "Commit or stage" in suggestion


def test_truncation_suggestion_locked_files_no_diff_advisory():
    suggestion = _truncation_suggestion("locked_files", [])
    assert "--include-workdir" in suggestion


def test_truncation_suggestion_locked_files_with_diff_advisory():
    suggestion = _truncation_suggestion(
        "locked_files", [{"kind": "empty_diff"}]
    )
    assert "--files <subpath>" in suggestion


def test_truncation_suggestion_workdir():
    suggestion = _truncation_suggestion("workdir", [])
    assert "smaller logical chunks" in suggestion


# ---------------------------------------------------------------------------
# Fallback chain via build_review_packet_payload (mocked git)
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
                 degrees=None, tested=None):
        self._nodes = nodes or []
        self._callers = callers or []
        self._callees = callees or []
        self._imports = imports or []
        self._degrees = degrees or []
        self._tested = tested or []

    def execute(self, query, *, parameters=None):
        q = query.lower()
        # _find_tested_uids: RETURN ... caller.path AS caller_path
        if "caller_path" in q:
            return _FakeResult(self._tested)
        if "match (caller)" in q and "calls" in q:
            return _FakeResult(self._callers)
        if "match (source)" in q and "calls" in q:
            return _FakeResult(self._callees)
        if "imports" in q:
            return _FakeResult(self._imports)
        if "count(caller)" in q:
            return _FakeResult(self._degrees)
        # Default: node lookup
        return _FakeResult(self._nodes)


@patch("codegraphcontext_ext.commands.review_packet._run_git")
def test_fallback_diff_source(mock_git):
    """When base..head produces files, source should be 'diff'."""
    def git_side_effect(*args, cwd=None):
        if args[0] == "rev-parse" and args[1] == "--verify":
            return "abc123"
        if args[0] == "merge-base":
            return ""
        if args[0] == "diff" and "--name-only" in args:
            return "src/auth.py\nsrc/billing.py"
        if args[0] == "diff" and "--shortstat" in args:
            return " 2 files changed, 30 insertions(+), 5 deletions(-)"
        return ""

    mock_git.side_effect = git_side_effect

    with patch("codegraphcontext_ext.commands.review_packet._is_ancestor", return_value=True):
        payload = build_review_packet_payload(base="main", head="feature")

    assert payload["source"] == "diff"
    assert payload["base"] == "main"
    assert payload["head"] == "feature"
    assert payload["diff_stats"]["files"] == 2


@patch("codegraphcontext_ext.commands.review_packet._run_git")
def test_fallback_staged_source(mock_git):
    """When no base/head but staged files exist, source is 'staged'."""
    call_count = {"diff_name": 0}

    def git_side_effect(*args, cwd=None):
        if args[0] == "diff" and "--cached" in args and "--name-only" in args:
            return "src/app.py"
        if args[0] == "diff" and "--cached" in args and "--shortstat" in args:
            return " 1 file changed, 10 insertions(+)"
        return ""

    mock_git.side_effect = git_side_effect

    payload = build_review_packet_payload()
    assert payload["source"] == "staged"
    assert payload["base"] is None


@patch("codegraphcontext_ext.commands.review_packet._run_git")
def test_fallback_workdir_source(mock_git):
    """When nothing staged, falls back to workdir."""
    def git_side_effect(*args, cwd=None):
        if args[0] == "diff" and "--cached" in args:
            return ""
        if args[0] == "diff" and "--name-only" in args:
            return "src/utils.py"
        if args[0] == "diff" and "--shortstat" in args:
            return " 1 file changed, 5 insertions(+)"
        return ""

    mock_git.side_effect = git_side_effect

    payload = build_review_packet_payload()
    assert payload["source"] == "workdir"


@patch("codegraphcontext_ext.commands.review_packet._run_git")
def test_fallback_untracked_source(mock_git):
    """When nothing else, falls back to untracked files."""
    def git_side_effect(*args, cwd=None):
        if args[0] == "diff":
            return ""
        if args[0] == "ls-files":
            return "new_file.py"
        return ""

    mock_git.side_effect = git_side_effect

    payload = build_review_packet_payload()
    assert payload["source"] == "untracked"
    kinds = {a["kind"] for a in payload["advisories"]}
    assert "untracked_only" in kinds


@patch("codegraphcontext_ext.commands.review_packet._run_git")
def test_fallback_locked_files_source(mock_git):
    """Last resort: use --files lock list."""
    def git_side_effect(*args, cwd=None):
        if args[0] == "diff":
            return ""
        if args[0] == "ls-files":
            return ""
        return ""

    mock_git.side_effect = git_side_effect

    payload = build_review_packet_payload(files=["src/auth.py"])
    assert payload["source"] == "locked_files"
    kinds = {a["kind"] for a in payload["advisories"]}
    assert "no_diff_available" in kinds


@patch("codegraphcontext_ext.commands.review_packet._run_git")
def test_fallback_nothing_available(mock_git):
    """No files at all, no --files."""
    mock_git.return_value = ""

    payload = build_review_packet_payload()
    assert payload["source"] == "locked_files"
    assert payload["diff_stats"]["files"] == 0
    assert payload["touched_nodes"] == []


# ---------------------------------------------------------------------------
# Include flags
# ---------------------------------------------------------------------------


@patch("codegraphcontext_ext.commands.review_packet._run_git")
def test_include_staged_forces_source(mock_git):
    def git_side_effect(*args, cwd=None):
        if "--cached" in args and "--name-only" in args:
            return "staged_file.py"
        if "--cached" in args and "--shortstat" in args:
            return " 1 file changed, 3 insertions(+)"
        return ""

    mock_git.side_effect = git_side_effect

    payload = build_review_packet_payload(include_staged=True)
    assert payload["source"] == "staged"


@patch("codegraphcontext_ext.commands.review_packet._run_git")
def test_include_workdir_forces_source(mock_git):
    def git_side_effect(*args, cwd=None):
        if args[0] == "diff" and "--name-only" in args and "--cached" not in args:
            return "workdir_file.py"
        if args[0] == "diff" and "--shortstat" in args and "--cached" not in args:
            return " 1 file changed, 2 deletions(-)"
        return ""

    mock_git.side_effect = git_side_effect

    payload = build_review_packet_payload(include_workdir=True)
    assert payload["source"] == "workdir"


@patch("codegraphcontext_ext.commands.review_packet._run_git")
def test_include_untracked_forces_source(mock_git):
    def git_side_effect(*args, cwd=None):
        if args[0] == "ls-files":
            return "brand_new.py"
        return ""

    mock_git.side_effect = git_side_effect

    payload = build_review_packet_payload(include_untracked=True)
    assert payload["source"] == "untracked"


# ---------------------------------------------------------------------------
# Conflicting flags (CLI level)
# ---------------------------------------------------------------------------


def test_conflicting_include_flags_exits_1():
    app = _review_app()
    result = runner.invoke(app, [
        "review-packet",
        "--include-staged", "--include-workdir",
    ])
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Graph integration (mocked connection)
# ---------------------------------------------------------------------------


@patch("codegraphcontext_ext.commands.review_packet._run_git")
def test_graph_nodes_in_payload(mock_git):
    """Touched nodes from graph appear in output."""
    mock_git.return_value = ""

    conn = _FakeConn(
        nodes=[
            ("uid1", "verify_token", "src/auth.py", 42, "Function"),
            ("uid2", "AuthService", "src/auth.py", 10, "Class"),
        ],
    )

    payload = build_review_packet_payload(
        files=["src/auth.py"], conn=conn,
    )
    assert len(payload["touched_nodes"]) == 2
    assert payload["touched_nodes"][0]["name"] == "verify_token"


@patch("codegraphcontext_ext.commands.review_packet._run_git")
def test_callers_not_in_diff(mock_git):
    """Callers from outside the diff files appear."""
    mock_git.return_value = ""

    conn = _FakeConn(
        nodes=[("uid1", "verify_token", "src/auth.py", 42, "Function")],
        callers=[("uid_pay", "pay_out", "src/billing.py", 99, "Function")],
    )

    payload = build_review_packet_payload(
        files=["src/auth.py"], conn=conn,
    )
    assert len(payload["callers_not_in_diff"]) == 1
    assert payload["callers_not_in_diff"][0]["name"] == "pay_out"
    assert payload["callers_not_in_diff"][0]["untested"] is True


@patch("codegraphcontext_ext.commands.review_packet._run_git")
def test_callees_not_in_diff(mock_git):
    """Callees from outside the diff files appear."""
    mock_git.return_value = ""

    conn = _FakeConn(
        nodes=[("uid1", "verify_token", "src/auth.py", 42, "Function")],
        callees=[("uid_db", "db_query", "src/db.py", 15, "Function")],
    )

    payload = build_review_packet_payload(
        files=["src/auth.py"], conn=conn,
    )
    assert len(payload["callees_not_in_diff"]) == 1
    assert payload["callees_not_in_diff"][0]["name"] == "db_query"


@patch("codegraphcontext_ext.commands.review_packet._run_git")
def test_cross_module_impact(mock_git):
    """Cross-module impact from IMPORTS edges."""
    mock_git.return_value = ""

    conn = _FakeConn(
        imports=[("billing.payment",)],
    )

    payload = build_review_packet_payload(
        files=["src/auth.py"], conn=conn,
    )
    assert "billing" in payload["cross_module_impact"]


@patch("codegraphcontext_ext.commands.review_packet._run_git")
def test_relative_changed_paths_match_absolute_graph_nodes(mock_git, tmp_path):
    """Git-relative paths should still match graph nodes stored with absolute paths."""
    mock_git.return_value = ""

    repo = tmp_path / "repo"
    repo.mkdir()
    abs_path = repo / "src" / "auth.py"
    abs_path.parent.mkdir(parents=True)
    abs_path.write_text("def verify_token():\n    return True\n", encoding="utf-8")

    conn = _FakeConn(
        nodes=[("uid1", "verify_token", str(abs_path), 1, "Function")],
    )

    payload = build_review_packet_payload(
        files=["src/auth.py"], conn=conn, cwd=repo,
    )
    assert len(payload["touched_nodes"]) == 1
    assert payload["touched_nodes"][0]["file"] == "src/auth.py:1"


# ---------------------------------------------------------------------------
# Truncation (§4.4)
# ---------------------------------------------------------------------------


@patch("codegraphcontext_ext.commands.review_packet._run_git")
def test_truncation_with_cap(mock_git):
    """When nodes exceed max_nodes, truncated flag and counts appear."""
    mock_git.return_value = ""

    nodes = [(f"uid{i}", f"fn{i}", "src/big.py", i, "Function") for i in range(60)]
    conn = _FakeConn(nodes=nodes)

    payload = build_review_packet_payload(
        files=["src/big.py"], conn=conn, max_nodes=10,
    )

    assert payload["truncated"] is True
    assert payload["total_nodes"]["touched"] == 60
    assert payload["returned_nodes"]["touched"] == 10
    assert len(payload["touched_nodes"]) == 10

    kinds = {a["kind"] for a in payload["advisories"]}
    assert "packet_truncated" in kinds


@patch("codegraphcontext_ext.commands.review_packet._run_git")
def test_no_truncation_below_cap(mock_git):
    """When under cap, no truncation fields."""
    mock_git.return_value = ""

    nodes = [(f"uid{i}", f"fn{i}", "src/small.py", i, "Function") for i in range(5)]
    conn = _FakeConn(nodes=nodes)

    payload = build_review_packet_payload(
        files=["src/small.py"], conn=conn, max_nodes=50,
    )

    assert "truncated" not in payload


# ---------------------------------------------------------------------------
# Advisories
# ---------------------------------------------------------------------------


@patch("codegraphcontext_ext.commands.review_packet._run_git")
def test_missing_base_ref_advisory(mock_git):
    """Unresolvable --base produces missing_base_ref advisory."""
    def git_side_effect(*args, cwd=None):
        if args[0] == "rev-parse" and "--verify" in args:
            from subprocess import CalledProcessError
            raise CalledProcessError(128, "git")
        # Fall through to staged
        if args[0] == "diff" and "--cached" in args and "--name-only" in args:
            return "file.py"
        if args[0] == "diff" and "--cached" in args and "--shortstat" in args:
            return " 1 file changed, 1 insertion(+)"
        return ""

    mock_git.side_effect = git_side_effect

    payload = build_review_packet_payload(base="deleted-branch", head="HEAD")
    kinds = {a["kind"] for a in payload["advisories"]}
    assert "missing_base_ref" in kinds
    # Still produced output via fallback
    assert payload["source"] == "staged"


@patch("codegraphcontext_ext.commands.review_packet._run_git")
def test_empty_diff_advisory(mock_git):
    """When refs resolve but diff is empty, advisory is emitted and fallback continues."""
    def git_side_effect(*args, cwd=None):
        if args[0] == "rev-parse" and "--verify" in args:
            return "abc123"
        if args[0] == "diff" and "--name-only" in args and ".." in args[-1]:
            return ""  # empty diff
        if args[0] == "diff" and "--shortstat" in args and ".." in args[-1]:
            return ""
        # Staged fallback
        if args[0] == "diff" and "--cached" in args and "--name-only" in args:
            return "staged.py"
        if args[0] == "diff" and "--cached" in args and "--shortstat" in args:
            return " 1 file changed, 2 insertions(+)"
        return ""

    mock_git.side_effect = git_side_effect

    with patch("codegraphcontext_ext.commands.review_packet._is_ancestor", return_value=True):
        payload = build_review_packet_payload(base="main", head="feature")

    kinds = {a["kind"] for a in payload["advisories"]}
    assert "empty_diff" in kinds
    assert payload["source"] == "staged"


@patch("codegraphcontext_ext.commands.review_packet._run_git")
def test_refs_diverged_advisory(mock_git):
    """Non-ancestor base produces refs_diverged_from_main advisory."""
    def git_side_effect(*args, cwd=None):
        if args[0] == "rev-parse" and "--verify" in args:
            return "abc123"
        if args[0] == "diff" and "--name-only" in args:
            return "src/auth.py"
        if args[0] == "diff" and "--shortstat" in args:
            return " 1 file changed, 5 insertions(+)"
        return ""

    mock_git.side_effect = git_side_effect

    with patch("codegraphcontext_ext.commands.review_packet._is_ancestor", return_value=False):
        payload = build_review_packet_payload(base="main", head="feature")

    kinds = {a["kind"] for a in payload["advisories"]}
    assert "refs_diverged_from_main" in kinds
    assert payload["source"] == "diff"


@patch("codegraphcontext_ext.commands.review_packet._run_git")
def test_excluded_by_cgcignore_advisory(mock_git, tmp_path):
    """Paths excluded by .cgcignore are called out explicitly."""
    mock_git.return_value = ""

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".cgcignore").write_text("ignored.py\n", encoding="utf-8")
    (repo / "ignored.py").write_text("print('hi')\n", encoding="utf-8")

    payload = build_review_packet_payload(
        files=["ignored.py"], cwd=repo,
    )

    advisory = next(a for a in payload["advisories"] if a["kind"] == "excluded_by_cgcignore")
    assert "ignored.py" in advisory["detail"]


@patch("codegraphcontext_ext.commands.review_packet._run_git")
def test_unsupported_repo_shape_bare_repo_advisory(mock_git):
    """Bare repositories degrade to locked_files with an explicit advisory."""
    def git_side_effect(*args, cwd=None):
        if args[:2] == ("rev-parse", "--is-inside-work-tree"):
            return "false"
        if args[:2] == ("rev-parse", "--is-bare-repository"):
            return "true"
        if args[:2] == ("rev-parse", "--show-superproject-working-tree"):
            return ""
        return ""

    mock_git.side_effect = git_side_effect

    payload = build_review_packet_payload(files=["src/auth.py"])
    assert payload["source"] == "locked_files"
    advisory = next(a for a in payload["advisories"] if a["kind"] == "unsupported_repo_shape")
    assert "bare_repo" in advisory["detail"]


@patch("codegraphcontext_ext.commands.review_packet._run_git")
def test_stale_index_advisory_for_workdir_changes(mock_git, tmp_path):
    """Tracked workdir changes with graph coverage emit stale_index."""
    repo = tmp_path / "repo"
    repo.mkdir()
    rel_path = "src/auth.py"
    abs_path = repo / rel_path
    abs_path.parent.mkdir(parents=True)
    abs_path.write_text("def verify_token():\n    return False\n", encoding="utf-8")

    def git_side_effect(*args, cwd=None):
        if args[0] == "diff" and "--cached" in args:
            return ""
        if args[0] == "diff" and "--name-only" in args and "--cached" not in args:
            return rel_path
        if args[0] == "diff" and "--shortstat" in args and "--cached" not in args:
            return " 1 file changed, 1 insertion(+), 1 deletion(-)"
        if args[0] == "hash-object":
            return "newhash"
        if args[0] == "rev-parse" and args[1] == f"HEAD:{rel_path}":
            return "oldhash"
        return ""

    mock_git.side_effect = git_side_effect

    conn = _FakeConn(
        nodes=[("uid1", "verify_token", str(abs_path), 1, "Function")],
    )

    payload = build_review_packet_payload(conn=conn, cwd=repo)
    advisory = next(a for a in payload["advisories"] if a["kind"] == "stale_index")
    assert rel_path in advisory["detail"]


# ---------------------------------------------------------------------------
# Graceful degradation (no graph connection)
# ---------------------------------------------------------------------------


@patch("codegraphcontext_ext.commands.review_packet._run_git")
def test_no_graph_connection_still_produces_output(mock_git):
    """Without a KùzuDB connection, graph fields are empty but JSON is valid."""
    mock_git.return_value = ""

    payload = build_review_packet_payload(files=["src/auth.py"], conn=None)
    assert payload["source"] == "locked_files"
    assert payload["touched_nodes"] == []
    assert payload["callers_not_in_diff"] == []
    assert payload["callees_not_in_diff"] == []
    assert payload["cross_module_impact"] == []


# ---------------------------------------------------------------------------
# Output shape invariants
# ---------------------------------------------------------------------------


@patch("codegraphcontext_ext.commands.review_packet._run_git")
def test_output_always_has_required_keys(mock_git):
    """Every payload has the spec-required top-level keys."""
    mock_git.return_value = ""

    payload = build_review_packet_payload()
    required = {
        "source", "base", "head", "diff_stats",
        "touched_nodes", "callers_not_in_diff", "callees_not_in_diff",
        "cross_module_impact", "advisories",
    }
    assert required.issubset(payload.keys())


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def test_review_packet_registered():
    """review-packet appears in the Typer app commands."""
    app = _review_app()
    command_names = [cmd.name for cmd in app.registered_commands]
    # register_extensions adds to the app; check the callback list
    # The command may be in the registered_commands or in the Typer groups
    result = runner.invoke(app, ["review-packet", "--help"])
    assert result.exit_code == 0
    assert "review-packet" in result.output.lower() or "reviewer" in result.output.lower()


@patch("codegraphcontext_ext.commands.review_packet.get_kuzu_connection")
@patch("codegraphcontext_ext.commands.review_packet._run_git")
def test_cli_basic_invocation(mock_git, mock_conn):
    """Basic CLI invocation produces valid JSON."""
    mock_git.return_value = ""
    mock_conn.side_effect = Exception("no db")

    app = _review_app()
    result = runner.invoke(app, [
        "review-packet", "--files", "src/auth.py",
    ])
    assert result.exit_code == 0
    payload = _extract_json(result.output)
    assert "source" in payload
    assert "advisories" in payload


@patch("codegraphcontext_ext.commands.review_packet.get_kuzu_connection")
@patch("codegraphcontext_ext.commands.review_packet._run_git")
def test_cli_max_nodes_option(mock_git, mock_conn):
    """--max-nodes is accepted."""
    mock_git.return_value = ""
    mock_conn.side_effect = Exception("no db")

    app = _review_app()
    result = runner.invoke(app, [
        "review-packet", "--files", "src/auth.py", "--max-nodes", "10",
    ])
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Regression: uid in caller/callee dicts (fix 3)
# ---------------------------------------------------------------------------


@patch("codegraphcontext_ext.commands.review_packet._run_git")
def test_callers_carry_uid(mock_git):
    """Callers must include uid so truncation can rank by in-degree."""
    mock_git.return_value = ""

    conn = _FakeConn(
        nodes=[("uid1", "verify_token", "src/auth.py", 42, "Function")],
        callers=[("uid_pay", "pay_out", "src/billing.py", 99, "Function")],
    )

    payload = build_review_packet_payload(
        files=["src/auth.py"], conn=conn,
    )
    assert "uid" in payload["callers_not_in_diff"][0]
    assert payload["callers_not_in_diff"][0]["uid"] == "uid_pay"


@patch("codegraphcontext_ext.commands.review_packet._run_git")
def test_callees_carry_uid(mock_git):
    """Callees must include uid so truncation can rank by in-degree."""
    mock_git.return_value = ""

    conn = _FakeConn(
        nodes=[("uid1", "verify_token", "src/auth.py", 42, "Function")],
        callees=[("uid_db", "db_query", "src/db.py", 15, "Function")],
    )

    payload = build_review_packet_payload(
        files=["src/auth.py"], conn=conn,
    )
    assert "uid" in payload["callees_not_in_diff"][0]
    assert payload["callees_not_in_diff"][0]["uid"] == "uid_db"


# ---------------------------------------------------------------------------
# Regression: untested_caller uses test-coverage evidence (fix 1)
# ---------------------------------------------------------------------------


@patch("codegraphcontext_ext.commands.review_packet._run_git")
def test_caller_not_untested_when_graph_has_test_caller(mock_git):
    """A caller with a test-file caller in the graph is not untested."""
    mock_git.return_value = ""

    conn = _FakeConn(
        nodes=[("uid1", "verify_token", "src/auth.py", 42, "Function")],
        callers=[("uid_bill", "charge", "src/billing.py", 10, "Function")],
        # Graph shows uid_bill is called from a test file
        tested=[("uid_bill", "tests/test_billing.py")],
    )

    payload = build_review_packet_payload(
        files=["src/auth.py"], conn=conn,
    )
    caller = payload["callers_not_in_diff"][0]
    assert caller["untested"] is False


@patch("codegraphcontext_ext.commands.review_packet._run_git")
def test_caller_untested_when_no_test_in_diff(mock_git):
    """A caller with no matching test file in the diff is untested."""
    mock_git.return_value = ""

    conn = _FakeConn(
        nodes=[("uid1", "verify_token", "src/auth.py", 42, "Function")],
        callers=[("uid_bill", "charge", "src/billing.py", 10, "Function")],
    )

    # No test file for billing in the diff
    payload = build_review_packet_payload(
        files=["src/auth.py"], conn=conn,
    )
    caller = payload["callers_not_in_diff"][0]
    assert caller["untested"] is True


# ---------------------------------------------------------------------------
# Regression: untracked_unindexed_omitted advisory (fix 2)
# ---------------------------------------------------------------------------


@patch("codegraphcontext_ext.commands.review_packet._run_git")
def test_unindexed_files_emit_advisory(mock_git):
    """Files not in the graph produce an untracked_unindexed_omitted advisory."""
    mock_git.return_value = ""

    # Graph has nodes for auth.py but not for brand_new.py
    conn = _FakeConn(
        nodes=[("uid1", "verify_token", "src/auth.py", 42, "Function")],
    )

    payload = build_review_packet_payload(
        files=["src/auth.py", "src/brand_new.py"], conn=conn,
    )
    kinds = {a["kind"] for a in payload["advisories"]}
    assert "untracked_unindexed_omitted" in kinds
    detail = next(a["detail"] for a in payload["advisories"]
                  if a["kind"] == "untracked_unindexed_omitted")
    assert "brand_new.py" in detail


@patch("codegraphcontext_ext.commands.review_packet._run_git")
def test_no_unindexed_advisory_when_all_indexed(mock_git):
    """No advisory when all changed files have graph nodes."""
    mock_git.return_value = ""

    conn = _FakeConn(
        nodes=[("uid1", "verify_token", "src/auth.py", 42, "Function")],
    )

    payload = build_review_packet_payload(
        files=["src/auth.py"], conn=conn,
    )
    kinds = {a["kind"] for a in payload["advisories"]}
    assert "untracked_unindexed_omitted" not in kinds


@patch("codegraphcontext_ext.commands.review_packet._run_git")
def test_unindexed_advisory_mentions_worktree_synthesis(mock_git):
    """The untracked_unindexed_omitted advisory names the missing feature."""
    mock_git.return_value = ""

    conn = _FakeConn(
        nodes=[("uid1", "fn", "src/a.py", 1, "Function")],
    )

    payload = build_review_packet_payload(
        files=["src/a.py", "src/new.py"], conn=conn,
    )
    detail = next(a["detail"] for a in payload["advisories"]
                  if a["kind"] == "untracked_unindexed_omitted")
    assert "synthesized" in detail
    assert "new.py" in detail


# ---------------------------------------------------------------------------
# _is_test_path
# ---------------------------------------------------------------------------


def test_is_test_path_prefix():
    assert _is_test_path("tests/test_billing.py") is True
    assert _is_test_path("test_billing.py") is True


def test_is_test_path_suffix():
    assert _is_test_path("billing_test.py") is True


def test_is_test_path_in_tests_dir():
    assert _is_test_path("tests/integration/billing.py") is True
    assert _is_test_path("src/test/billing.py") is True


def test_is_test_path_negative():
    assert _is_test_path("src/billing.py") is False
    assert _is_test_path("src/attestation.py") is False
    assert _is_test_path("") is False


# ---------------------------------------------------------------------------
# _synthesize_nodes_from_file
# ---------------------------------------------------------------------------


def test_synthesize_nodes_from_py_file(tmp_path):
    """Worktree synthesis extracts functions and classes from a .py file."""
    py = tmp_path / "new_module.py"
    py.write_text("def foo():\n    pass\n\nclass Bar:\n    pass\n")

    nodes = _synthesize_nodes_from_file(str(py))
    names = {n["name"] for n in nodes}
    assert "foo" in names
    assert "Bar" in names
    assert all(n.get("uid") for n in nodes)


def test_synthesize_nodes_non_python():
    """Non-Python files return empty — they can't be synthesized."""
    assert _synthesize_nodes_from_file("src/config.json") == []
    assert _synthesize_nodes_from_file("README.md") == []


def test_synthesize_nodes_missing_file():
    assert _synthesize_nodes_from_file("does/not/exist.py") == []


# ---------------------------------------------------------------------------
# Worktree synthesis integration
# ---------------------------------------------------------------------------


@patch("codegraphcontext_ext.commands.review_packet._run_git")
def test_worktree_synthesis_adds_nodes(mock_git, tmp_path):
    """Synthesized nodes from unindexed .py files appear in touched_nodes."""
    mock_git.return_value = ""

    py_file = tmp_path / "brand_new.py"
    py_file.write_text("def hello():\n    pass\n")

    conn = _FakeConn(
        nodes=[("uid1", "fn", "src/a.py", 1, "Function")],
    )

    payload = build_review_packet_payload(
        files=["src/a.py", str(py_file)], conn=conn,
    )
    node_names = {n["name"] for n in payload["touched_nodes"]}
    assert "hello" in node_names
    # No omitted advisory since the .py file was synthesized
    kinds = {a["kind"] for a in payload["advisories"]}
    assert "untracked_unindexed_omitted" not in kinds
