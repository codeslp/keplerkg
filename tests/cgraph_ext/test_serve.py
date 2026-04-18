"""Tests for the Phase 3 warm daemon (serve command)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from codegraphcontext_ext.config import CgraphConfig
from codegraphcontext_ext.daemon.serve import (
    COMMAND_NAME,
    SUMMARY,
    _dispatch,
    _handle_client,
    _run_server,
    default_socket_path,
)


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def test_command_metadata():
    assert COMMAND_NAME == "serve"
    assert isinstance(SUMMARY, str) and len(SUMMARY) > 0


# ---------------------------------------------------------------------------
# default_socket_path
# ---------------------------------------------------------------------------

def test_default_socket_path_no_xdg(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    path = default_socket_path()
    assert path.name == "ipc.sock"
    assert ".cache/cgraph" in str(path)


def test_default_socket_path_with_xdg(monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    path = default_socket_path()
    assert str(path) == "/run/user/1000/cgraph/ipc.sock"


# ---------------------------------------------------------------------------
# _dispatch
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG = CgraphConfig()


def test_dispatch_advise():
    with patch(
        "codegraphcontext_ext.commands.advise.resolve_cgraph_config",
        return_value=_DEFAULT_CONFIG,
    ):
        result = _dispatch("advise", {"situation": "lock_overlap", "lane": "b"})
    assert result["situation"] == "lock_overlap"
    assert result["advisory_id"] is not None


def test_dispatch_unknown():
    result = _dispatch("nonexistent", {})
    assert result["ok"] is False
    assert result["kind"] == "unknown_command"


def test_dispatch_blast_radius():
    """blast-radius dispatch with no DB returns empty payload with advisory."""
    with patch(
        "codegraphcontext_ext.commands.blast_radius.get_kuzu_connection",
        side_effect=Exception("no db"),
    ):
        result = _dispatch("blast-radius", {"files": ["src/foo.py"]})
    assert result["ok"] is True
    # Should have a no_graph advisory
    assert any(a["kind"] == "no_graph" for a in result.get("advisories", []))


# ---------------------------------------------------------------------------
# Async server integration test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_server_round_trip(tmp_path):
    """Start the daemon, send a request, verify the response."""
    # Use /tmp directly — tmp_path can exceed the 104-byte AF_UNIX limit
    import tempfile
    short_dir = Path(tempfile.mkdtemp(prefix="cg_"))
    sock_path = short_dir / "t.sock"

    # Start server in background
    server_task = asyncio.create_task(_run_server(sock_path))
    # Wait for socket to appear
    for _ in range(50):
        if sock_path.exists():
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("Server socket did not appear")

    try:
        reader, writer = await asyncio.open_unix_connection(str(sock_path))

        # Send advise request
        request = {"cmd": "advise", "args": {"situation": "lock_overlap", "lane": "a"}}
        with patch(
            "codegraphcontext_ext.commands.advise.resolve_cgraph_config",
            return_value=_DEFAULT_CONFIG,
        ):
            writer.write((json.dumps(request) + "\n").encode())
            await writer.drain()

            raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
        response = json.loads(raw)
        assert response["situation"] == "lock_overlap"
        assert response["advisory_id"] is not None

        # Send unknown command
        writer.write((json.dumps({"cmd": "nope"}) + "\n").encode())
        await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
        response = json.loads(raw)
        assert response["ok"] is False

        # Send invalid JSON
        writer.write(b"not json\n")
        await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
        response = json.loads(raw)
        assert response["kind"] == "parse_error"

        writer.close()
    finally:
        server_task.cancel()
        try:
            await server_task
        except asyncio.CancelledError:
            pass
        import shutil
        shutil.rmtree(short_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# CLI registration
# ---------------------------------------------------------------------------

def test_serve_registered():
    """serve command is registered in the CLI."""
    from codegraphcontext_ext.cli import register_extensions
    import typer

    app = typer.Typer()
    register_extensions(app)
    command_names = [cmd.name for cmd in app.registered_commands]
    assert "serve" in command_names
