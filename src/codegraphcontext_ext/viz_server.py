"""Shared bits for commands that run a local HTTP server for viz assets.

`cgc viz-projector` and `cgc viz-dashboard` both need to:
  - copy the vendored Projector build into a tempdir
  - write our vectors.tsv + metadata.tsv + projector_config.json
  - pick a free port
  - run `http.server` in a thread and block the main thread on Ctrl-C
  - clean up the tempdir on exit

All of that lives here so the two commands stay thin.
"""

from __future__ import annotations

import http.server
import json
import shutil
import socket
import socketserver
import sys
import threading
import time
import webbrowser
from importlib import resources
from pathlib import Path
from typing import Any, Optional

_VENDOR_PACKAGE = "codegraphcontext_ext.viz_assets.projector"
# index.html references cgraph-patch.css + cgraph-patch.js via relative paths;
# all four ship together.
VENDOR_FILES: tuple[str, ...] = (
    "index.html",
    "favicon.png",
    "cgraph-patch.css",
    "cgraph-patch.js",
)
DATA_SUBDIR = "cgraph_data"  # mirrors the patched projector-config-json-path


def copy_vendored_projector(dest: Path) -> None:
    """Copy the vendored Projector files (index.html + favicon) into *dest*."""
    dest.mkdir(parents=True, exist_ok=True)
    for name in VENDOR_FILES:
        with resources.as_file(resources.files(_VENDOR_PACKAGE).joinpath(name)) as src:
            shutil.copy2(src, dest / name)


def _sanitize_tsv_cell(value: Any) -> str:
    if value is None:
        return ""
    s = str(value)
    return s.replace("\t", " ").replace("\n", " ").replace("\r", " ")


def write_projector_data(data_dir: Path, nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """Write vectors.tsv + metadata.tsv + projector_config.json into *data_dir*.

    Returns the config dict.  Safe to call with an empty *nodes* list: an
    empty tensor + header-only metadata is produced.
    """
    data_dir.mkdir(parents=True, exist_ok=True)

    vectors_path = data_dir / "vectors.tsv"
    with vectors_path.open("w", encoding="utf-8") as f:
        for n in nodes:
            f.write("\t".join(f"{v:.6f}" for v in n["embedding"]))
            f.write("\n")

    metadata_path = data_dir / "metadata.tsv"
    with metadata_path.open("w", encoding="utf-8") as f:
        f.write("name\ttype\tpath\tline\n")
        for n in nodes:
            f.write(
                "\t".join(
                    _sanitize_tsv_cell(v)
                    for v in (n["name"], n["type"], n["path"], n["line"])
                )
            )
            f.write("\n")

    rows = len(nodes)
    dims = len(nodes[0]["embedding"]) if nodes else 0

    config = {
        "embeddings": [
            {
                "tensorName": "cgraph code embeddings",
                "tensorShape": [rows, dims],
                "tensorPath": f"{DATA_SUBDIR}/vectors.tsv",
                "metadataPath": f"{DATA_SUBDIR}/metadata.tsv",
            }
        ],
        "modelCheckpointPath": "cgraph",
    }
    (data_dir / "projector_config.json").write_text(
        json.dumps(config, indent=2), encoding="utf-8"
    )
    return config


def find_free_port(preferred: Optional[int] = None) -> int:
    """Return *preferred* if it binds, otherwise let the kernel pick."""
    if preferred:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", preferred))
                return preferred
            except OSError:
                pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler with no-cache headers + quiet stdout.

    No-cache headers matter here: Chrome otherwise caches `http://127.0.0.1`
    responses aggressively, and when the dev restarts the server (new tempdir,
    same URL), the browser serves a stale iframe and our patches silently
    don't apply.  We're always serving fresh files, so send the headers that
    tell the browser to fetch them.
    """

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, *_args, **_kwargs):  # noqa: D401
        return


def build_server(directory: Path, port: int) -> socketserver.TCPServer:
    """Return a TCPServer rooted at *directory* bound to 127.0.0.1:*port*."""
    def handler_factory(*args, **kwargs):
        return _QuietHandler(*args, directory=str(directory), **kwargs)

    socketserver.TCPServer.allow_reuse_address = True
    return socketserver.TCPServer(("127.0.0.1", port), handler_factory)


def serve_until_interrupted(
    server: socketserver.TCPServer,
    url: str,
    *,
    no_open: bool,
    cleanup_dir: Optional[Path] = None,
) -> None:
    """Run *server* on a background thread and block until Ctrl-C.

    Opens *url* in the default browser unless *no_open* is set.  Removes
    *cleanup_dir* at the end so tempdirs don't leak after Ctrl-C.
    """
    thread = threading.Thread(
        target=server.serve_forever,
        name="cgraph-viz-http",
        daemon=True,
    )
    thread.start()

    if not no_open:
        time.sleep(0.2)  # let the socket settle before the first request
        webbrowser.open(url)

    try:
        while thread.is_alive():
            thread.join(timeout=0.5)
    except KeyboardInterrupt:
        print("\nStopping viz server...", file=sys.stderr)
    finally:
        server.shutdown()
        server.server_close()
        if cleanup_dir is not None:
            shutil.rmtree(cleanup_dir, ignore_errors=True)
