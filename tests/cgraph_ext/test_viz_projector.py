"""Tests for cgc viz-projector — TF Embedding Projector served locally.

The blocking `serve_forever` loop is not exercised here; this covers the
deterministic pieces (vendor copy, TSV/config generation, free-port helper,
CLI registration, no-embeddings error path, prepared-tempdir layout).
"""

from __future__ import annotations

import json
import socket
from unittest.mock import patch

from typer.testing import CliRunner

from codegraphcontext_ext.commands.viz_projector import _prepare_projector_serve_dir
from codegraphcontext_ext.viz_server import (
    DATA_SUBDIR,
    VENDOR_FILES,
    _sanitize_tsv_cell,
    build_server,
    copy_vendored_projector,
    find_free_port,
    write_projector_data,
)

from .conftest import (
    FakeResult,
    build_ext_app,
    extract_last_json,
    mark_kuzu_backend_available,
)


def test_viz_projector_registered():
    names = [cmd.name for cmd in build_ext_app().registered_commands]
    assert "viz-projector" in names


def test_vendored_projector_files_present():
    """Package data ships all four vendored assets: index, favicon, patch css/js."""
    from importlib import resources
    pkg = resources.files("codegraphcontext_ext.viz_assets.projector")
    for name in VENDOR_FILES:
        assert pkg.joinpath(name).is_file(), f"missing vendored asset: {name}"
    # Guardrail: the tuple must include the two cgraph-owned patch files so
    # the index.html link/script tags resolve after copy.
    assert "cgraph-patch.css" in VENDOR_FILES
    assert "cgraph-patch.js" in VENDOR_FILES

    index_html = pkg.joinpath("index.html").read_text(encoding="utf-8")

    # Patch 1: config path points at our data, not upstream demos.
    assert 'projector-config-json-path="cgraph_data/projector_config.json"' in index_html
    assert 'projector-config-json-path="oss_data/oss_demo_projector_config.json"' not in index_html

    # Patch 2: hasWebGLSupport drops the weblas gate (that check silently
    # fails on modern Chrome and produces a misleading "no WebGL" error).
    assert '"undefined"!==typeof weblas}' not in index_html, (
        "upstream weblas gate must be dropped"
    )
    assert "[cgraph-patch] hasWebGLSupport" in index_html, (
        "patched diagnostic log must be present"
    )

    # Patch 3: cgraph-patch.css + .js are pulled in before <body>.
    assert 'href="cgraph-patch.css"' in index_html
    assert 'src="cgraph-patch.js"' in index_html

    # Patch CSS must define the cgraph-dark palette + .cgraph-simple hides.
    css = pkg.joinpath("cgraph-patch.css").read_text(encoding="utf-8")
    assert "--cgraph-bg" in css
    assert ".cgraph-simple" in css

    # Patch JS drives the auto-tweaks (night mode, Z axis, simple class).
    js = pkg.joinpath("cgraph-patch.js").read_text(encoding="utf-8")
    assert "cgraph-simple" in js
    assert "brightness-2" in js  # night-mode toggle selector
    assert "advanced=1" in js or "advanced" in js  # URL opt-out recognized


def test_copy_vendored_projector_writes_all_files(tmp_path):
    copy_vendored_projector(tmp_path)
    for name in VENDOR_FILES:
        assert (tmp_path / name).is_file(), f"missing after copy: {name}"
    assert (tmp_path / "index.html").stat().st_size > 100_000  # ~1.8 MB


def test_write_projector_data_round_trip(tmp_path):
    nodes = [
        {"name": "foo", "type": "Function", "path": "a.py", "line": 1,
         "embedding": [0.1, 0.2, 0.3, 0.4]},
        {"name": "Bar", "type": "Class", "path": "b.py", "line": 42,
         "embedding": [0.5, 0.6, 0.7, 0.8]},
    ]
    data_dir = tmp_path / DATA_SUBDIR

    config = write_projector_data(data_dir, nodes)

    assert config["embeddings"][0]["tensorShape"] == [2, 4]
    assert config["embeddings"][0]["tensorPath"] == f"{DATA_SUBDIR}/vectors.tsv"
    assert config["embeddings"][0]["metadataPath"] == f"{DATA_SUBDIR}/metadata.tsv"

    written = json.loads((data_dir / "projector_config.json").read_text())
    assert written == config

    vector_lines = (data_dir / "vectors.tsv").read_text().strip().splitlines()
    assert len(vector_lines) == 2
    assert [float(x) for x in vector_lines[0].split("\t")] == [0.1, 0.2, 0.3, 0.4]

    meta_lines = (data_dir / "metadata.tsv").read_text().strip().splitlines()
    assert meta_lines[0] == "name\ttype\tpath\tline"
    assert meta_lines[1].split("\t") == ["foo", "Function", "a.py", "1"]
    assert meta_lines[2].split("\t") == ["Bar", "Class", "b.py", "42"]


def test_write_projector_data_sanitizes_tab_and_newline(tmp_path):
    nodes = [{
        "name": "foo\twith\ttabs",
        "type": "Function",
        "path": "a\nb.py",
        "line": 1,
        "embedding": [0.1, 0.2],
    }]
    data_dir = tmp_path / DATA_SUBDIR
    write_projector_data(data_dir, nodes)

    meta_lines = (data_dir / "metadata.tsv").read_text().strip().splitlines()
    assert len(meta_lines) == 2
    cells = meta_lines[1].split("\t")
    assert len(cells) == 4
    assert "\t" not in cells[0] and "\n" not in cells[0]
    assert "\n" not in cells[2]


def test_sanitize_tsv_cell_handles_none_and_cr():
    assert _sanitize_tsv_cell(None) == ""
    assert _sanitize_tsv_cell("a\rb") == "a b"
    assert _sanitize_tsv_cell(42) == "42"


def test_write_projector_data_empty_nodes_writes_zero_shape(tmp_path):
    data_dir = tmp_path / DATA_SUBDIR
    config = write_projector_data(data_dir, [])
    assert config["embeddings"][0]["tensorShape"] == [0, 0]
    assert (data_dir / "vectors.tsv").read_text() == ""
    assert (data_dir / "metadata.tsv").read_text().strip() == "name\ttype\tpath\tline"


def test_find_free_port_returns_kernel_assigned_when_no_preferred():
    port = find_free_port()
    assert 1024 <= port <= 65535


def test_find_free_port_falls_back_when_preferred_is_bound():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        busy_port = s.getsockname()[1]
        fallback = find_free_port(busy_port)
        assert fallback != busy_port
        assert 1024 <= fallback <= 65535


def test_prepare_projector_serve_dir_layout(tmp_path, monkeypatch):
    """_prepare_projector_serve_dir stages everything the Projector needs."""
    # Force mkdtemp into tmp_path so we can clean up deterministically.
    import tempfile
    monkeypatch.setattr(tempfile, "mkdtemp", lambda **_kw: str(tmp_path))

    nodes = [
        {"name": "foo", "type": "Function", "path": "a.py", "line": 1,
         "embedding": [0.1, 0.2, 0.3]},
    ]
    serve_dir, config = _prepare_projector_serve_dir(nodes)

    # Vendored files at the root.
    assert (serve_dir / "index.html").is_file()
    assert (serve_dir / "favicon.png").is_file()
    # Our tensor + metadata + config under cgraph_data/.
    assert (serve_dir / DATA_SUBDIR / "vectors.tsv").is_file()
    assert (serve_dir / DATA_SUBDIR / "metadata.tsv").is_file()
    assert (serve_dir / DATA_SUBDIR / "projector_config.json").is_file()
    assert config["embeddings"][0]["tensorShape"] == [1, 3]


def test_build_server_starts_and_serves_index(tmp_path):
    """build_server + find_free_port produce a server that actually responds.

    We don't use serve_until_interrupted here because that blocks; instead
    drive serve_forever on a daemon thread and make a single request.
    """
    import threading
    import urllib.request

    (tmp_path / "index.html").write_text("<!DOCTYPE html>hello cgraph", encoding="utf-8")

    port = find_free_port()
    server = build_server(tmp_path, port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as resp:
            body = resp.read().decode("utf-8")
        assert "hello cgraph" in body
    finally:
        server.shutdown()
        server.server_close()


def test_viz_projector_no_embeddings_returns_typed_error(monkeypatch):
    mark_kuzu_backend_available(monkeypatch)

    class _EmptyConn:
        def execute(self, _q, **_kw):
            return FakeResult([])

    with patch(
        "codegraphcontext_ext.commands.viz_projector.get_kuzu_connection",
        return_value=_EmptyConn(),
    ):
        result = CliRunner().invoke(build_ext_app(), ["viz-projector", "--no-open"])

    assert result.exit_code == 1
    payload = extract_last_json(result.output)
    assert payload["kind"] == "no_embeddings"
