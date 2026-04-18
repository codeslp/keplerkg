"""Tests for cgc viz-dashboard — server-backed 4-tab dashboard.

The blocking serve_forever is not exercised here; tests verify the
deterministic pre-serve setup: dashboard HTML shape, srcdoc iframe
escaping, Projector tempdir layout, and the typed error paths.
"""

from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner

from codegraphcontext_ext.commands.viz_dashboard import (
    _dashboard_html,
    _prepare_dashboard_serve_dir,
)
from codegraphcontext_ext.viz_server import DATA_SUBDIR

from .conftest import (
    FakeResult,
    build_ext_app,
    extract_last_json,
    mark_kuzu_backend_available,
)

runner = CliRunner()


class _DashboardConn:
    """Responds to graph-fetch + embedding-fetch queries against one Function rowset."""

    def __init__(self, node_rows, emb_rows=()):
        self._node_rows = list(node_rows)
        self._emb_rows = list(emb_rows)
        self._emb_served = False

    def execute(self, query, **_kwargs):
        if "IS NOT NULL" in query and "`Function`" in query and not self._emb_served:
            self._emb_served = True
            return FakeResult(self._emb_rows)
        if "`Function`" in query and "RETURN n.uid" in query:
            return FakeResult(self._node_rows)
        return FakeResult([])


def test_viz_dashboard_registered():
    app = build_ext_app()
    names = [cmd.name for cmd in app.registered_commands]
    assert "viz-dashboard" in names


def test_viz_dashboard_empty_graph_returns_typed_error(monkeypatch):
    mark_kuzu_backend_available(monkeypatch)

    with patch(
        "codegraphcontext_ext.commands.viz_dashboard.get_kuzu_connection",
        return_value=_DashboardConn([]),
    ):
        result = runner.invoke(build_ext_app(), ["viz-dashboard", "--no-open", "--port", "0"])

    assert result.exit_code == 1
    payload = extract_last_json(result.output)
    assert payload["kind"] == "empty_graph"


def test_viz_dashboard_rejects_unknown_layout(monkeypatch):
    mark_kuzu_backend_available(monkeypatch)
    result = runner.invoke(
        build_ext_app(),
        ["viz-dashboard", "--no-open", "--layout", "tree"],
    )
    assert result.exit_code != 0
    assert "unknown layout" in result.output or "tree" in result.output


def test_dashboard_html_wires_three_tabs_including_embeddings():
    graph = {
        "nodes": [{"id": "u1", "name": "foo", "path": "a.py", "line": 1, "type": "Function"}],
        "edges": [],
    }
    html = _dashboard_html(graph, 0, layout="cose")

    # Three tabs: 2D Graph, 3D Graph, Embeddings (which now IS the Projector).
    assert 'data-pane="pane-2d"' in html
    assert 'data-pane="pane-3d"' in html
    assert 'data-pane="pane-embeddings"' in html
    # Old scatter + separate Projector panes are gone.
    assert 'data-pane="pane-emb"' not in html
    assert 'data-pane="pane-projector"' not in html
    assert 'data-pane="pane-export"' not in html

    # 2D pane is visible on load, so srcdoc is applied immediately.
    # 3D pane is hidden on load — its content is stashed in data-srcdoc and
    # promoted on first tab click (Chrome won't grant WebGL to an invisible
    # iframe).  Embeddings iframe has no src at all until click for the same
    # reason; the Projector caches a "no WebGL" verdict once and forever.
    assert 'srcdoc="&lt;!DOCTYPE html&gt;' in html, "2D iframe must have srcdoc set up-front"
    assert 'data-srcdoc=' in html, "3D iframe must stash its HTML in data-srcdoc"
    assert 'id="iframe-3d"' in html
    assert 'id="emb-iframe"' in html
    # JS still references "projector/" — just not in a rendered src attribute.
    assert '"projector/"' in html
    # Tab label text is "Embeddings", not "Projector".
    assert ">Embeddings</button>" in html
    assert ">Projector</button>" not in html

    # Embeddings pane header + mode toggle are present.
    assert "Each dot is a function." in html
    assert 'id="emb-simple-btn"' in html
    assert 'id="emb-advanced-btn"' in html
    # Advanced mode re-loads the Projector with ?advanced=1 (opt-out signal
    # that cgraph-patch.js reads to skip the simple-mode body class).
    assert "?advanced=1" in html


def test_dashboard_html_srcdoc_escapes_inner_html():
    """Inner HTMLs contain literal </script> and quotes; srcdoc must escape them."""
    graph = {
        "nodes": [{"id": "u1", "name": "</script>", "path": "a.py", "line": 1, "type": "Function"}],
        "edges": [],
    }
    html = _dashboard_html(graph, 0, layout="cose")
    assert "&lt;/script&gt;" in html


def test_prepare_dashboard_serve_dir_layout(tmp_path, monkeypatch):
    """_prepare_dashboard_serve_dir stages the dashboard + Projector side-by-side."""
    import tempfile
    monkeypatch.setattr(tempfile, "mkdtemp", lambda **_kw: str(tmp_path))

    graph = {
        "nodes": [{"id": "u1", "name": "foo", "path": "a.py", "line": 1, "type": "Function"}],
        "edges": [],
    }
    emb_nodes = [{"name": "foo", "type": "Function", "path": "a.py", "line": 1,
                  "embedding": [0.1, 0.2, 0.3]}]

    serve_dir = _prepare_dashboard_serve_dir(graph, emb_nodes, layout="cose")

    assert (serve_dir / "index.html").is_file()
    chrome = (serve_dir / "index.html").read_text()
    assert "cgraph" in chrome
    # iframe src is set lazily from JS on first Embeddings-tab click; the
    # literal "projector/" path is still in the dashboard HTML as the JS value.
    assert '"projector/"' in chrome

    assert (serve_dir / "projector" / "index.html").is_file()
    assert (serve_dir / "projector" / "favicon.png").is_file()
    assert (serve_dir / "projector" / DATA_SUBDIR / "vectors.tsv").is_file()
    assert (serve_dir / "projector" / DATA_SUBDIR / "metadata.tsv").is_file()
    cfg = (serve_dir / "projector" / DATA_SUBDIR / "projector_config.json").read_text()
    assert '"tensorShape": [\n    1,\n    3\n  ]' in cfg or '"tensorShape": [1, 3]' in cfg or \
           '"tensorShape":[1,3]' in cfg or '[\n        1,\n        3\n      ]' in cfg, (
        f"tensor shape [1, 3] not found in config:\n{cfg}"
    )
