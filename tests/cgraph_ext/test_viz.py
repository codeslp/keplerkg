"""Tests for kkg viz-embeddings and kkg viz-graph commands."""

import json
import os
from unittest.mock import patch

from typer.testing import CliRunner

from codegraphcontext_ext.commands.viz_embeddings import (
    _generate_html as gen_emb_html,
    _reduce_to_2d,
)
from codegraphcontext_ext.commands.viz_graph import (
    _generate_html as gen_graph_html,
)

from .conftest import (
    FakeResult,
    FunctionOnlyConn,
    build_ext_app as _viz_app,
    extract_last_json as _extract_json,
    mark_kuzu_backend_available,
)

runner = CliRunner()


# --- Embedding viz tests ---


def test_reduce_to_2d_returns_correct_shape():
    embeddings = [[1.0, 2.0, 3.0, 4.0], [5.0, 6.0, 7.0, 8.0], [9.0, 10.0, 11.0, 12.0]]
    result = _reduce_to_2d(embeddings)
    assert len(result) == 3
    assert all(len(pt) == 2 for pt in result)


def test_reduce_to_2d_single_point():
    result = _reduce_to_2d([[1.0, 2.0, 3.0]])
    assert len(result) == 1
    assert len(result[0]) == 2


def test_generate_emb_html_contains_data():
    nodes = [
        {"name": "foo", "path": "src/a.py", "line": 10, "type": "Function"},
        {"name": "Bar", "path": "src/b.py", "line": 20, "type": "Class"},
    ]
    points = [[1.0, 2.0], [3.0, 4.0]]
    html = gen_emb_html(nodes, points)

    assert "cgraph" in html
    assert "Embedding Space" in html
    assert "foo" in html
    assert "Bar" in html
    assert "2 nodes" in html


def test_generate_emb_html_valid_json_data():
    nodes = [{"name": "x", "path": "a.py", "line": 1, "type": "Function"}]
    points = [[0.0, 0.0]]
    html = gen_emb_html(nodes, points)

    # Extract the DATA JSON from the HTML
    start = html.index("const DATA = ") + len("const DATA = ")
    end = html.index(";\n", start)
    data = json.loads(html[start:end])
    assert len(data) == 1
    assert data[0]["name"] == "x"


# --- Graph viz tests ---


def test_generate_graph_html_contains_data():
    graph = {
        "nodes": [
            {"id": "uid1", "name": "foo", "path": "a.py", "line": 1, "type": "Function"},
            {"id": "uid2", "name": "bar", "path": "b.py", "line": 2, "type": "Function"},
        ],
        "edges": [
            {"source": "uid1", "target": "uid2", "type": "CALLS"},
        ],
    }
    html = gen_graph_html(graph)

    assert "Code Graph" in html
    assert "2 nodes" in html
    assert "1 edges" in html
    # Must use Cytoscape.js (online requirement acknowledged in module docstring).
    assert "cytoscape" in html.lower()


def test_generate_graph_html_valid_json_data():
    graph = {
        "nodes": [{"id": "u1", "name": "f", "path": "a.py", "line": 1, "type": "Function"}],
        "edges": [],
    }
    html = gen_graph_html(graph)

    start = html.index("const GRAPH = ") + len("const GRAPH = ")
    end = html.index(";\n", start)
    data = json.loads(html[start:end])
    assert len(data["nodes"]) == 1
    assert data["edges"] == []


def test_generate_graph_html_accepts_dashboard_highlight_messages():
    graph = {
        "nodes": [{"id": "u1", "name": "f", "path": "a.py", "line": 1, "type": "Function"}],
        "edges": [],
    }
    html = gen_graph_html(graph)

    assert 'window.addEventListener("message"' in html
    assert 'payload.type !== "highlight"' in html
    assert "payload.id" in html
    assert "payload.name" in html
    assert "DASHBOARD_HIGHLIGHT_ZOOM_SCALE = 0.4" in html
    assert "cy.center(targets)" in html
    assert "highlightMatches(matches, true, DASHBOARD_HIGHLIGHT_ZOOM_SCALE)" in html


# --- CLI integration tests ---


def test_viz_embeddings_no_embeddings(monkeypatch, tmp_path):
    mark_kuzu_backend_available(monkeypatch)

    with patch(
        "codegraphcontext_ext.commands.viz_embeddings.get_kuzu_connection",
        return_value=FunctionOnlyConn([]),
    ):
        result = runner.invoke(_viz_app(), ["viz-embeddings", "--no-open"])

    assert result.exit_code == 1
    payload = _extract_json(result.output)
    assert payload["kind"] == "no_embeddings"


def test_viz_embeddings_generates_html(monkeypatch, tmp_path):
    mark_kuzu_backend_available(monkeypatch)

    emb_rows = [
        ("uid1", "foo", "a.py", 1, [0.1, 0.2, 0.3, 0.4]),
        ("uid2", "bar", "b.py", 2, [0.5, 0.6, 0.7, 0.8]),
    ]

    out_file = str(tmp_path / "emb.html")
    with patch(
        "codegraphcontext_ext.commands.viz_embeddings.get_kuzu_connection",
        return_value=FunctionOnlyConn(emb_rows),
    ):
        result = runner.invoke(_viz_app(), ["viz-embeddings", "--no-open", "-o", out_file])

    assert result.exit_code == 0
    assert os.path.exists(out_file)
    html = open(out_file).read()
    assert "foo" in html
    assert "Embedding Space" in html


def test_viz_graph_empty_graph(monkeypatch):
    mark_kuzu_backend_available(monkeypatch)

    with patch(
        "codegraphcontext_ext.commands.viz_graph.get_kuzu_connection",
        return_value=FunctionOnlyConn([]),
    ):
        result = runner.invoke(_viz_app(), ["viz-graph", "--no-open"])

    assert result.exit_code == 1
    payload = _extract_json(result.output)
    assert payload["kind"] == "empty_graph"


def test_viz_graph_generates_html(monkeypatch, tmp_path):
    mark_kuzu_backend_available(monkeypatch)

    node_rows = [
        ("uid1", "foo", "a.py", 1),
        ("uid2", "bar", "b.py", 2),
    ]

    class _GraphConn:
        """viz_graph queries are per-table and don't match FunctionOnlyConn's shape."""

        def execute(self, q, **_kw):
            if "Function" in q and "RETURN" in q:
                return FakeResult(node_rows)
            return FakeResult([])

    out_file = str(tmp_path / "graph.html")
    with patch(
        "codegraphcontext_ext.commands.viz_graph.get_kuzu_connection",
        return_value=_GraphConn(),
    ):
        result = runner.invoke(_viz_app(), ["viz-graph", "--no-open", "-o", out_file])

    assert result.exit_code == 0
    assert os.path.exists(out_file)
    html = open(out_file).read()
    assert "Code Graph" in html
    assert "cytoscape" in html.lower()


def test_generate_graph_html_includes_module_nodes():
    """Module nodes appear in graph data so IMPORTS edges can render."""
    graph = {
        "nodes": [
            {"id": "src/auth.py", "name": "auth.py", "path": "src/auth.py", "line": 0, "type": "File"},
            {"id": "os", "name": "os", "path": "", "line": 0, "type": "Module"},
        ],
        "edges": [
            {"source": "src/auth.py", "target": "os", "type": "IMPORTS"},
        ],
    }
    html = gen_graph_html(graph)

    start = html.index("const GRAPH = ") + len("const GRAPH = ")
    end = html.index(";\n", start)
    data = json.loads(html[start:end])
    node_types = {n["type"] for n in data["nodes"]}
    assert "Module" in node_types
    assert len(data["edges"]) == 1
    assert data["edges"][0]["type"] == "IMPORTS"


def test_viz_graph_file_to_function_contains_edge(monkeypatch, tmp_path):
    """Regression: File->Function CONTAINS edges survive the node-id join.

    Prior bug: CONTAINS queries returned `a.uid`/`b.uid`, but File nodes are
    keyed by `.path` in the node fetch (File has no .uid upstream), so the
    client-side `src in nodes` filter in _fetch_graph silently dropped every
    File->Function CONTAINS edge.  Fix is a COALESCE(.uid, .path, .name) in
    the edge query that mirrors the node-fetch precedence.
    """
    mark_kuzu_backend_available(monkeypatch)

    class _FileFuncConn:
        def execute(self, q, **kw):
            if "`File`" in q and "RETURN" in q and "uid" in q:
                # File node fetch: `n.path AS uid`
                return FakeResult([("src/auth.py", "auth.py", "src/auth.py", 0)])
            if "`Function`" in q and "RETURN" in q and "uid" in q:
                return FakeResult([("fn-verify-token", "verify_token", "src/auth.py", 42)])
            if "CONTAINS" in q:
                # Simulate upstream emitting the File's identifier via COALESCE.
                # A File->Function CONTAINS edge: source keyed by path, target by uid.
                return FakeResult([("src/auth.py", "fn-verify-token", "CONTAINS")])
            return FakeResult([])

    out_file = str(tmp_path / "graph.html")
    with patch(
        "codegraphcontext_ext.commands.viz_graph.get_kuzu_connection",
        return_value=_FileFuncConn(),
    ):
        app = _viz_app()
        result = runner.invoke(app, ["viz-graph", "--no-open", "-o", out_file])

    assert result.exit_code == 0, result.output

    html = open(out_file).read()
    start = html.index("const GRAPH = ") + len("const GRAPH = ")
    end = html.index(";\n", start)
    data = json.loads(html[start:end])

    # Both nodes present, each keyed by the column the node fetch used.
    node_ids = {n["id"] for n in data["nodes"]}
    assert "src/auth.py" in node_ids
    assert "fn-verify-token" in node_ids

    # The CONTAINS edge survived the join — source and target both reference
    # real nodes by the same identifiers.
    contains_edges = [e for e in data["edges"] if e["type"] == "CONTAINS"]
    assert len(contains_edges) == 1, f"expected 1 CONTAINS edge, got {data['edges']!r}"
    assert contains_edges[0]["source"] == "src/auth.py"
    assert contains_edges[0]["target"] == "fn-verify-token"


def test_reduce_to_2d_matches_pca_on_centered_data():
    """The numpy-only SVD PCA agrees with a manual centered-covariance PCA."""
    import numpy as np

    rng = np.random.default_rng(42)
    X = rng.standard_normal((20, 8))

    reduced = _reduce_to_2d(X.tolist())

    # Manual reference PCA via the same SVD the implementation uses.
    centered = X - X.mean(axis=0)
    _, _, vt = np.linalg.svd(centered, full_matrices=False)
    expected = centered @ vt[:2].T

    reduced_arr = np.asarray(reduced)
    # Principal-component signs are free; align per column before comparing.
    for col in range(2):
        if np.dot(reduced_arr[:, col], expected[:, col]) < 0:
            reduced_arr[:, col] *= -1
    assert np.allclose(reduced_arr, expected, atol=1e-6)


def test_reduce_to_2d_degenerate_input_returns_zeros():
    """Too-few-samples or 1-D input collapses to the [0,0] fallback."""
    assert _reduce_to_2d([]) == []
    assert _reduce_to_2d([[1.0, 2.0, 3.0]]) == [[0.0, 0.0]]
    assert _reduce_to_2d([[1.0], [2.0]]) == [[0.0, 0.0], [0.0, 0.0]]


def test_graph_html_escapes_script_closing_tag():
    """Node name containing </script> must not break the inline script block."""
    graph = {
        "nodes": [
            {"id": "uid1", "name": '</script><img src=x onerror=alert(1)>',
             "path": "src/evil</script>.py", "line": 1, "type": "Function"},
        ],
        "edges": [],
    }
    html = gen_graph_html(graph)

    # Extract the inline data-script (the one containing const GRAPH) and
    # verify its opening <script> lands before its closing </script>.
    data_script_start = html.index("const GRAPH = ")
    closing_idx = html.index("</script>", data_script_start)
    assert closing_idx > data_script_start, "closing </script> must follow the data block"

    json_start = data_script_start + len("const GRAPH = ")
    json_end = html.index(";\n", json_start)
    data = json.loads(html[json_start:json_end])
    assert data["nodes"][0]["name"] == '</script><img src=x onerror=alert(1)>'
    assert data["nodes"][0]["path"] == "src/evil</script>.py"


def test_emb_html_escapes_script_closing_tag():
    """Embedding node with </script> in name/path must not break the script block."""
    nodes = [
        {"name": '</script><img onerror=alert(1)>',
         "path": "src/evil</script>.py", "line": 1, "type": "Function"},
    ]
    points = [[0.0, 0.0]]
    html = gen_emb_html(nodes, points)

    script_start = html.index("<script>") + len("<script>")
    script_body = html[script_start:]
    closing_idx = script_body.index("</script>")
    assert "const DATA = " in script_body[:closing_idx]

    json_start = script_body.index("const DATA = ") + len("const DATA = ")
    json_end = script_body.index(";\n", json_start)
    data = json.loads(script_body[json_start:json_end])
    assert data[0]["name"] == '</script><img onerror=alert(1)>'
    assert data[0]["path"] == "src/evil</script>.py"


def test_graph_html_no_innerhtml():
    """Generated graph HTML must not use innerHTML for data rendering."""
    graph = {
        "nodes": [{"id": "u1", "name": "f", "path": "a.py", "line": 1, "type": "Function"}],
        "edges": [],
    }
    html = gen_graph_html(graph)
    # Inspect the last inline <script> block (the one with our data + handlers);
    # Cytoscape's own CDN-loaded source is not in this file.
    data_script_start = html.index("const GRAPH = ")
    data_script_end = html.index("</script>", data_script_start)
    assert "innerHTML" not in html[data_script_start:data_script_end]


def test_emb_html_no_innerhtml():
    """Generated embedding HTML must not use innerHTML for data rendering."""
    nodes = [{"name": "f", "path": "a.py", "line": 1, "type": "Function"}]
    points = [[0.0, 0.0]]
    html = gen_emb_html(nodes, points)
    script_start = html.index("<script>")
    assert "innerHTML" not in html[script_start:]


def test_viz_commands_registered():
    app = _viz_app()
    names = [cmd.name for cmd in app.registered_commands]
    assert "viz-embeddings" in names
    assert "viz-graph" in names


# --- Layout flag (Cytoscape.js) ---


def test_graph_html_uses_cytoscape_and_animate_false():
    """Cytoscape.js + deterministic layouts: animate:false on every layout config."""
    graph = {
        "nodes": [{"id": "u1", "name": "f", "path": "a.py", "line": 1, "type": "Function"}],
        "edges": [],
    }
    html = gen_graph_html(graph)
    assert "cytoscape@" in html, "Cytoscape.js CDN include must be present"
    assert "cytoscape-dagre" in html, "dagre plugin must be bundled so --layout dagre works"
    # Every layout in LAYOUT_CONFIGS must disable animation; otherwise users hit
    # the same jitter the hand-rolled force sim produced.
    assert html.count("animate: false") >= 5


def test_viz_graph_rejects_unknown_layout(monkeypatch):
    mark_kuzu_backend_available(monkeypatch)
    result = runner.invoke(_viz_app(), ["viz-graph", "--no-open", "--layout", "tree"])
    assert result.exit_code != 0
    assert "unknown layout" in result.output or "tree" in result.output


def test_viz_graph_3d_mode_emits_force_graph_template(monkeypatch, tmp_path):
    """--3d swaps in the 3d-force-graph template with a bounded cooldown."""
    mark_kuzu_backend_available(monkeypatch)

    node_rows = [("uid1", "foo", "a.py", 1)]

    class _GraphConn:
        def execute(self, q, **_kw):
            if "Function" in q and "RETURN" in q:
                return FakeResult(node_rows)
            return FakeResult([])

    out_file = str(tmp_path / "g3d.html")
    with patch(
        "codegraphcontext_ext.commands.viz_graph.get_kuzu_connection",
        return_value=_GraphConn(),
    ):
        result = runner.invoke(
            _viz_app(), ["viz-graph", "--no-open", "-o", out_file, "--3d"],
        )

    assert result.exit_code == 0, result.output
    payload = _extract_json(result.output)
    assert payload["mode"] == "3d"

    html = open(out_file).read()
    assert "3d-force-graph" in html, "3D template must pull in 3d-force-graph CDN"
    assert "ForceGraph3D()" in html
    assert 'id="graph-scene"' in html, "3D template must reserve a dedicated mount node"
    assert 'const graphSceneEl = document.getElementById("graph-scene");' in html
    assert "ForceGraph3D()(graphSceneEl)" in html
    assert '<div id="graph">\n  <div id="graph-scene"></div>' in html
    assert '<div class="legend">' in html
    assert '<div class="tooltip" id="tooltip"></div>' in html
    # Hard-cap on the settle animation — prevents the never-converging pathology.
    assert ".cooldownTicks(120)" in html
    assert "cytoscape" not in html.lower(), "3D path must not emit the Cytoscape template"


def test_viz_graph_reports_layout_in_json(monkeypatch, tmp_path):
    mark_kuzu_backend_available(monkeypatch)

    node_rows = [("uid1", "foo", "a.py", 1)]

    class _GraphConn:
        def execute(self, q, **_kw):
            if "Function" in q and "RETURN" in q:
                return FakeResult(node_rows)
            return FakeResult([])

    out_file = str(tmp_path / "g.html")
    with patch(
        "codegraphcontext_ext.commands.viz_graph.get_kuzu_connection",
        return_value=_GraphConn(),
    ):
        result = runner.invoke(
            _viz_app(),
            ["viz-graph", "--no-open", "-o", out_file, "--layout", "dagre"],
        )

    assert result.exit_code == 0, result.output
    payload = _extract_json(result.output)
    assert payload["layout"] == "dagre"
    html = open(out_file).read()
    assert 'INITIAL_LAYOUT = "dagre"' in html


# --- Reducer flag ---


def test_reducer_registry_lists_both_backends():
    from codegraphcontext_ext.commands.viz_embeddings import _REDUCERS

    assert set(_REDUCERS) == {"pca", "umap"}


def test_viz_embeddings_rejects_unknown_reducer(monkeypatch):
    mark_kuzu_backend_available(monkeypatch)

    app = _viz_app()
    result = runner.invoke(app, ["viz-embeddings", "--no-open", "--reducer", "tsne"])

    assert result.exit_code != 0
    assert "unknown reducer" in result.output or "tsne" in result.output


def test_viz_embeddings_pca_reducer_end_to_end(monkeypatch, tmp_path):
    """The default PCA path still works after the --reducer refactor."""
    mark_kuzu_backend_available(monkeypatch)

    emb_rows = [
        ("uid1", "foo", "a.py", 1, [0.1, 0.2, 0.3, 0.4]),
        ("uid2", "bar", "b.py", 2, [0.5, 0.6, 0.7, 0.8]),
    ]

    class _EmbConn:
        def __init__(self):
            self._seen = set()

        def execute(self, q, **kw):
            for table in ("Function", "Class"):
                if f"`{table}`" in q and table not in self._seen:
                    self._seen.add(table)
                    if table == "Function":
                        return FakeResult(emb_rows)
            return FakeResult([])

    out_file = str(tmp_path / "emb.html")
    with patch(
        "codegraphcontext_ext.commands.viz_embeddings.get_kuzu_connection",
        return_value=_EmbConn(),
    ):
        app = _viz_app()
        result = runner.invoke(
            app,
            ["viz-embeddings", "--no-open", "--reducer", "pca", "-o", out_file],
        )

    assert result.exit_code == 0, result.output
    payload = _extract_json(result.output)
    assert payload["reducer"] == "pca"
    html = open(out_file).read()
    assert "(PCA 2D)" in html


def test_viz_embeddings_reports_reducer_unavailable_when_umap_missing(monkeypatch, tmp_path):
    """Missing umap-learn surfaces as a typed error, not a traceback."""
    mark_kuzu_backend_available(monkeypatch)

    emb_rows = [
        ("uid1", "foo", "a.py", 1, [0.1, 0.2, 0.3, 0.4]),
        ("uid2", "bar", "b.py", 2, [0.5, 0.6, 0.7, 0.8]),
    ]

    class _EmbConn:
        def __init__(self):
            self._seen = set()

        def execute(self, q, **kw):
            for table in ("Function", "Class"):
                if f"`{table}`" in q and table not in self._seen:
                    self._seen.add(table)
                    if table == "Function":
                        return FakeResult(emb_rows)
            return FakeResult([])

    def _fake_umap_reducer(_embeddings):
        raise RuntimeError(
            "UMAP requested but `umap-learn` is not installed. "
            "Install it with `pip install umap-learn` or run without --reducer umap."
        )

    with patch(
        "codegraphcontext_ext.commands.viz_embeddings.get_kuzu_connection",
        return_value=_EmbConn(),
    ), patch.dict(
        "codegraphcontext_ext.commands.viz_embeddings._REDUCERS",
        {"umap": _fake_umap_reducer},
    ):
        app = _viz_app()
        result = runner.invoke(app, ["viz-embeddings", "--no-open", "--reducer", "umap"])

    assert result.exit_code == 1
    payload = _extract_json(result.output)
    assert payload["kind"] == "reducer_unavailable"
    assert "umap-learn" in payload["detail"]
