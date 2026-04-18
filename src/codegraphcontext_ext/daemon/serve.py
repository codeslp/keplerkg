"""kkg serve: warm daemon over Unix socket to avoid Python cold-start.

Spec §3.4 — long-lived background process that accepts JSON-line
requests on a Unix domain socket and dispatches them to command
handlers in-process.  Eliminates the 100-300ms Python startup that
would otherwise blow the 200ms ``advise`` budget.

Socket path: ``~/.cache/cgraph/ipc.sock`` (or ``$XDG_RUNTIME_DIR/cgraph/ipc.sock``).

Protocol:
  → client sends one JSON line: ``{"cmd": "advise", "args": {...}}``
  ← server replies with one JSON line (the command's stdout payload)
  Connection stays open for pipelining; close to disconnect.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any, Optional

import typer

COMMAND_NAME = "serve"
SUMMARY = "Start warm daemon on a Unix socket to eliminate cold-start latency."

# ---------------------------------------------------------------------------
# Socket path resolution
# ---------------------------------------------------------------------------

def default_socket_path() -> Path:
    """Return the canonical socket path, respecting XDG_RUNTIME_DIR."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        base = Path(runtime) / "cgraph"
    else:
        base = Path.home() / ".cache" / "cgraph"
    return base / "ipc.sock"


# ---------------------------------------------------------------------------
# Command dispatch table
# ---------------------------------------------------------------------------

def _dispatch(cmd: str, args: dict[str, Any]) -> dict[str, Any]:
    """Route a request to the appropriate command handler.

    Each handler is imported lazily so the daemon only pays import cost
    once (on first call per command type).
    """
    if cmd == "advise":
        from ..commands.advise import build_advise_payload
        return build_advise_payload(
            situation=args.get("situation", ""),
            context=args.get("context"),
            lane=args.get("lane"),
        )
    if cmd == "blast-radius":
        from ..commands.blast_radius import build_blast_radius_payload
        return build_blast_radius_payload(
            files=args.get("files", []),
            lane=args.get("lane"),
            locks_json=args.get("locks_json"),
            max_nodes=args.get("max_nodes", 50),
        )
    if cmd == "drift-check":
        from ..commands.drift_check import build_drift_check_payload
        return build_drift_check_payload(
            files=args.get("files", []),
            since=args.get("since", ""),
            lane=args.get("lane"),
        )
    # Fallback: unknown command
    return {
        "ok": False,
        "kind": "unknown_command",
        "detail": f"Daemon does not handle command '{cmd}'.",
    }


# ---------------------------------------------------------------------------
# Async server
# ---------------------------------------------------------------------------

async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    """Handle one client connection — read JSON lines, dispatch, reply."""
    try:
        while True:
            raw = await reader.readline()
            if not raw:
                break  # Client disconnected
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError as exc:
                response = {"ok": False, "kind": "parse_error", "detail": str(exc)}
                writer.write((json.dumps(response) + "\n").encode())
                await writer.drain()
                continue

            cmd = request.get("cmd", "")
            args = request.get("args", {})
            try:
                result = _dispatch(cmd, args)
            except Exception as exc:
                result = {
                    "ok": False,
                    "kind": "internal_error",
                    "detail": f"{type(exc).__name__}: {exc}",
                }

            writer.write((json.dumps(result, sort_keys=True) + "\n").encode())
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        writer.close()


async def _run_server(socket_path: Path) -> None:
    """Start the Unix socket server and serve until interrupted."""
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    # Remove stale socket
    if socket_path.exists():
        socket_path.unlink()

    server = await asyncio.start_unix_server(_handle_client, path=str(socket_path))
    # Make socket accessible
    socket_path.chmod(0o600)

    print(f"cgraph daemon listening on {socket_path}", file=sys.stderr)

    # Graceful shutdown on SIGTERM/SIGINT
    loop = asyncio.get_running_loop()
    stop = loop.create_future()

    def _signal_handler() -> None:
        if not stop.done():
            stop.set_result(None)

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    try:
        await stop
    finally:
        server.close()
        await server.wait_closed()
        if socket_path.exists():
            socket_path.unlink()
        print("cgraph daemon stopped.", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def serve_command(
    socket: Optional[str] = typer.Option(
        None,
        "--socket",
        help="Unix socket path (default: ~/.cache/cgraph/ipc.sock).",
    ),
) -> None:
    """Start the cgraph warm daemon on a Unix domain socket."""
    sock_path = Path(socket) if socket else default_socket_path()
    try:
        asyncio.run(_run_server(sock_path))
    except KeyboardInterrupt:
        pass
    raise typer.Exit(code=0)
