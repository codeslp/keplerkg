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
from typing import Any, Callable, Optional

import typer

COMMAND_NAME = "serve"
SUMMARY = "Start warm daemon on a Unix socket to eliminate cold-start latency."
LOCALHOST_COMMAND_NAME = "serve-localhost"
LOCALHOST_SUMMARY = "Start warm daemon on localhost TCP and retry nearby ports until one binds."

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


def _install_stop_handlers(stop: asyncio.Future[None]) -> None:
    """Register SIGTERM/SIGINT to resolve *stop* exactly once."""
    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        if not stop.done():
            stop.set_result(None)

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)


async def _serve_until_stopped(
    server: asyncio.AbstractServer,
    *,
    location: str,
    cleanup: Optional[Callable[[], None]] = None,
) -> None:
    """Keep *server* running until SIGTERM/SIGINT, then clean up."""
    print(f"cgraph daemon listening on {location}", file=sys.stderr)

    stop: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    _install_stop_handlers(stop)

    try:
        await stop
    finally:
        server.close()
        await server.wait_closed()
        if cleanup is not None:
            cleanup()
        print("cgraph daemon stopped.", file=sys.stderr)


async def _start_tcp_server(
    *,
    host: str,
    port: int,
    retries: int,
) -> tuple[asyncio.AbstractServer, int]:
    """Bind a TCP daemon, retrying nearby ports when the preferred one is busy."""
    last_error: Optional[OSError] = None
    candidate_ports = [port] if port == 0 else range(port, port + retries + 1)

    for candidate_port in candidate_ports:
        try:
            server = await asyncio.start_server(_handle_client, host=host, port=candidate_port)
        except OSError as exc:
            last_error = exc
            continue

        sockets = server.sockets or []
        if not sockets:
            server.close()
            await server.wait_closed()
            raise RuntimeError("TCP daemon started without an exposed socket.")
        bound_port = int(sockets[0].getsockname()[1])
        return server, bound_port

    if last_error is not None:
        raise last_error
    raise RuntimeError("Failed to bind TCP daemon.")


async def _run_tcp_server(host: str, port: int, retries: int) -> None:
    """Start the localhost TCP daemon and serve until interrupted."""
    server, bound_port = await _start_tcp_server(host=host, port=port, retries=retries)
    await _serve_until_stopped(server, location=f"{host}:{bound_port}")


async def _run_server(socket_path: Path) -> None:
    """Start the Unix socket server and serve until interrupted."""
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    # Remove stale socket
    if socket_path.exists():
        socket_path.unlink()

    server = await asyncio.start_unix_server(_handle_client, path=str(socket_path))
    # Make socket accessible
    socket_path.chmod(0o600)
    await _serve_until_stopped(
        server,
        location=str(socket_path),
        cleanup=lambda: socket_path.exists() and socket_path.unlink(),
    )


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


def serve_localhost_command(
    port: int = typer.Option(
        8765,
        "--port",
        help="Preferred localhost port for the TCP daemon.",
    ),
    retries: int = typer.Option(
        10,
        "--retries",
        min=0,
        help="How many additional ports to try if the preferred port is busy.",
    ),
) -> None:
    """Start the cgraph warm daemon on localhost TCP with port retry."""
    try:
        asyncio.run(_run_tcp_server("127.0.0.1", port, retries))
    except KeyboardInterrupt:
        pass
    raise typer.Exit(code=0)
