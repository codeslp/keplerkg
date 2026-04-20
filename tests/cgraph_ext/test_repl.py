"""Tests for the kkg repl command (Phase 2.5)."""

from __future__ import annotations

from codegraphcontext_ext.commands.repl import (
    COMMAND_NAME,
    SUMMARY,
    ReplSession,
    _format_output,
    _handle_dot_command,
    run_repl_loop,
    _AVAILABLE_COMMANDS,
)


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def test_command_metadata():
    assert COMMAND_NAME == "repl"
    assert isinstance(SUMMARY, str) and len(SUMMARY) > 0


def test_available_commands():
    assert "search" in _AVAILABLE_COMMANDS
    assert "impact" in _AVAILABLE_COMMANDS
    assert "audit" in _AVAILABLE_COMMANDS
    assert "clusters" in _AVAILABLE_COMMANDS
    assert "entrypoints" in _AVAILABLE_COMMANDS
    assert "execution-flow" in _AVAILABLE_COMMANDS
    assert "manifest" in _AVAILABLE_COMMANDS


# ---------------------------------------------------------------------------
# ReplSession
# ---------------------------------------------------------------------------


def test_session_defaults():
    s = ReplSession()
    assert s.project is None
    assert s.profile == "default"
    assert len(s.history) == 0
    assert s.conn is None


def test_session_prompt_no_project():
    s = ReplSession()
    prompt = s.prompt()
    assert "kkg" in prompt
    # No project slug in the prompt (ANSI codes use [ but no project name)
    assert "[33m" not in prompt  # yellow project text not present


def test_session_prompt_with_project():
    s = ReplSession(project="flask")
    prompt = s.prompt()
    assert "flask" in prompt


def test_session_set_profile_valid():
    s = ReplSession()
    result = s.set_profile("strict")
    assert s.profile == "strict"
    assert "strict" in result


def test_session_set_profile_invalid():
    s = ReplSession()
    result = s.set_profile("nonexistent")
    assert s.profile == "default"  # unchanged
    assert "Unknown" in result


# ---------------------------------------------------------------------------
# Dot-commands
# ---------------------------------------------------------------------------


def test_dot_quit():
    s = ReplSession()
    assert _handle_dot_command(s, ".quit") is None
    assert _handle_dot_command(s, ".exit") is None
    assert _handle_dot_command(s, ".q") is None


def test_dot_project_show():
    s = ReplSession(project="flask")
    result = _handle_dot_command(s, ".project")
    assert "flask" in result


def test_dot_profile_show():
    s = ReplSession(profile="soc2")
    result = _handle_dot_command(s, ".profile")
    assert "soc2" in result


def test_dot_profile_set():
    s = ReplSession()
    result = _handle_dot_command(s, ".profile strict")
    assert s.profile == "strict"
    assert "strict" in result


def test_dot_history_empty():
    s = ReplSession()
    result = _handle_dot_command(s, ".history")
    assert "empty" in result


def test_dot_history_with_entries():
    s = ReplSession()
    s.history.append("search auth")
    s.history.append("impact login")
    result = _handle_dot_command(s, ".history")
    assert "search auth" in result
    assert "impact login" in result


def test_dot_commands():
    s = ReplSession()
    result = _handle_dot_command(s, ".commands")
    assert "search" in result
    assert "impact" in result
    assert ".project" in result


def test_dot_help():
    s = ReplSession()
    result = _handle_dot_command(s, ".help")
    assert "search" in result
    assert ".quit" in result


def test_dot_unknown():
    s = ReplSession()
    result = _handle_dot_command(s, ".foobar")
    assert "Unknown" in result


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def test_format_impact():
    payload = {
        "kind": "impact",
        "summary": {"matches": 1, "callers": 5, "callees": 3, "entrypoint_callers": 2, "entrypoint_callees": 0},
    }
    out = _format_output(payload)
    assert "1 match" in out
    assert "5 callers" in out
    assert "2 entry points" in out


def test_format_execution_flow():
    payload = {
        "kind": "execution_flow",
        "summary": {"total_nodes": 10, "total_edges": 9, "max_depth_reached": 3},
    }
    out = _format_output(payload)
    assert "10 nodes" in out
    assert "9 edges" in out


def test_format_clusters():
    payload = {
        "kind": "clusters",
        "stats": {"communities": 4, "total_nodes": 50, "cross_community_edges": 8},
    }
    out = _format_output(payload)
    assert "4 communities" in out
    assert "8 cross-boundary" in out


def test_format_audit_clean():
    payload = {"kind": "audit", "violations": []}
    out = _format_output(payload)
    assert "No violations" in out


def test_format_audit_with_violations():
    payload = {
        "kind": "audit",
        "violations": [{"offenders": [{"uid": "a"}, {"uid": "b"}]}],
    }
    out = _format_output(payload)
    assert "2 violation" in out


def test_format_entrypoints():
    payload = {
        "kind": "entrypoints",
        "entrypoints": [
            {"name": "login", "framework": "flask", "score": 6.5, "file": "src/auth.py:10"},
        ],
        "summary": {"total": 1},
    }
    out = _format_output(payload)
    assert "login" in out
    assert "flask" in out


def test_format_context_no_results():
    payload = {"kind": "context", "seeds": []}
    out = _format_output(payload)
    assert "No results" in out


def test_format_context_with_results():
    payload = {
        "kind": "context",
        "seeds": [
            {"name": "authenticate", "score": 0.92, "file": "src/auth.py:10"},
        ],
    }
    out = _format_output(payload)
    assert "authenticate" in out
    assert "0.920" in out


# ---------------------------------------------------------------------------
# REPL loop (with injected I/O)
# ---------------------------------------------------------------------------


def test_repl_loop_quit():
    """REPL exits on .quit."""
    inputs = iter([".quit"])
    outputs = []
    session = ReplSession(project="test")
    run_repl_loop(session, input_fn=lambda _: next(inputs), output_fn=outputs.append)
    assert any("interactive session" in o for o in outputs)


def test_repl_loop_eof():
    """REPL exits on EOFError (Ctrl-D)."""
    def raise_eof(_):
        raise EOFError
    outputs = []
    session = ReplSession()
    run_repl_loop(session, input_fn=raise_eof, output_fn=outputs.append)


def test_repl_loop_dot_command():
    """Dot-commands produce output."""
    inputs = iter([".profile", ".quit"])
    outputs = []
    session = ReplSession(profile="soc2")
    run_repl_loop(session, input_fn=lambda _: next(inputs), output_fn=outputs.append)
    assert any("soc2" in o for o in outputs)


def test_repl_loop_history_recorded():
    """Commands are recorded in history."""
    inputs = iter([".help", ".commands", ".quit"])
    outputs = []
    session = ReplSession()
    run_repl_loop(session, input_fn=lambda _: next(inputs), output_fn=outputs.append)
    assert ".help" in session.history
    assert ".commands" in session.history


def test_repl_loop_empty_line_skipped():
    """Empty lines don't crash or add to history."""
    inputs = iter(["", "  ", ".quit"])
    outputs = []
    session = ReplSession()
    run_repl_loop(session, input_fn=lambda _: next(inputs), output_fn=outputs.append)
    assert len(session.history) == 1  # only .quit
