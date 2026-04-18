"""cgc viz-projector: serve the vendored TF Embedding Projector against our embeddings.

Runs a local HTTP server with the Projector pre-loaded against the current
kuzu store's Function/Class embeddings.  Blocks until Ctrl-C; cleans up the
tempdir on exit.  See NOTICE in src/codegraphcontext_ext/viz_assets/projector
for the two patches applied to the vendored upstream.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import typer

from ..embeddings.fetch import fetch_embedded_nodes
from ..embeddings.runtime import probe_backend_support
from ..io.json_stdout import emit_json
from ..io.kuzu import get_kuzu_connection
from ..viz_server import (
    DATA_SUBDIR,
    build_server,
    copy_vendored_projector,
    find_free_port,
    serve_until_interrupted,
    write_projector_data,
)

COMMAND_NAME = "viz-projector"
SCHEMA_FILE = "context.json"
SUMMARY = "Serve the TF Embedding Projector locally with cgraph embeddings pre-loaded."


def _prepare_projector_serve_dir(nodes: list[dict]) -> tuple[Path, dict]:
    """Create a tempdir with vendored Projector + our tensor/metadata.

    Extracted from the command body so tests can exercise it without starting
    a server.  Returns (serve_dir, config).
    """
    serve_dir = Path(tempfile.mkdtemp(prefix="cgraph-projector-"))
    copy_vendored_projector(serve_dir)
    config = write_projector_data(serve_dir / DATA_SUBDIR, nodes)
    return serve_dir, config


def viz_projector_command(
    port: int = typer.Option(
        0,
        "--port", "-p",
        help="Port to bind (0 = let the kernel pick a free one).",
    ),
    no_open: bool = typer.Option(
        False,
        "--no-open",
        help="Start the server but don't open the browser.",
    ),
) -> None:
    """Serve the TF Embedding Projector locally with your cgraph embeddings pre-loaded."""

    backend_payload = probe_backend_support()
    if not backend_payload["ok"]:
        typer.echo(emit_json(backend_payload))
        raise typer.Exit(code=1)

    conn = get_kuzu_connection()
    print("Fetching embeddings...", file=sys.stderr)
    nodes = fetch_embedded_nodes(conn)

    if not nodes:
        typer.echo(emit_json({
            "ok": False,
            "kind": "no_embeddings",
            "detail": "No embedded nodes found. Run `cgc embed` first.",
        }))
        raise typer.Exit(code=1)

    serve_dir, config = _prepare_projector_serve_dir(nodes)
    bound_port = find_free_port(port or None)
    server = build_server(serve_dir, bound_port)
    url = f"http://127.0.0.1:{bound_port}/"

    typer.echo(emit_json({
        "ok": True,
        "kind": "viz_projector_serving",
        "nodes": len(nodes),
        "tensor_shape": config["embeddings"][0]["tensorShape"],
        "serve_dir": str(serve_dir),
        "url": url,
    }))
    print(
        f"\ncgraph projector: serving {len(nodes)} embeddings at {url}\n"
        f"(Ctrl-C to stop)",
        file=sys.stderr,
    )

    serve_until_interrupted(server, url, no_open=no_open, cleanup_dir=serve_dir)
    raise typer.Exit(code=0)
