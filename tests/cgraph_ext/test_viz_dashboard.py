"""Tests for kkg viz-dashboard — server-backed 4-tab dashboard.

The blocking serve_forever is not exercised here; tests verify the
deterministic pre-serve setup: dashboard HTML shape, srcdoc iframe
escaping, Projector tempdir layout, and the typed error paths.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch

import codegraphcontext_ext.commands.viz_dashboard as viz_dashboard_mod
from typer.testing import CliRunner

from codegraphcontext_ext.commands.viz_dashboard import (
    _collect_dashboard_count_details,
    _dashboard_html,
    _load_standards_json,
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
_CAPTURE_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "research"
    / "experiments"
    / "dogfooding"
    / "scripts"
    / "capture_graph_screenshots.py"
)
_CAPTURE_SPEC = importlib.util.spec_from_file_location(
    "capture_graph_screenshots", _CAPTURE_SCRIPT_PATH,
)
assert _CAPTURE_SPEC and _CAPTURE_SPEC.loader
capture_graph_screenshots = importlib.util.module_from_spec(_CAPTURE_SPEC)
_CAPTURE_SPEC.loader.exec_module(capture_graph_screenshots)


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


def test_viz_dashboard_routes_project_before_serving(monkeypatch, tmp_path):
    mark_kuzu_backend_available(monkeypatch)
    graph = {
        "nodes": [{"id": "u1", "name": "foo", "path": "a.py", "line": 1, "type": "Function"}],
        "edges": [],
    }

    with patch(
        "codegraphcontext_ext.commands.viz_dashboard.activate_project",
    ) as activate_project, patch(
        "codegraphcontext_ext.commands.viz_dashboard.get_kuzu_connection",
        return_value=object(),
    ), patch(
        "codegraphcontext_ext.commands.viz_dashboard._fetch_graph",
        return_value=graph,
    ), patch(
        "codegraphcontext_ext.commands.viz_dashboard.fetch_embedded_nodes",
        return_value=[],
    ), patch(
        "codegraphcontext_ext.commands.viz_dashboard._prepare_dashboard_serve_dir",
        return_value=tmp_path,
    ), patch(
        "codegraphcontext_ext.commands.viz_dashboard.find_free_port",
        return_value=43123,
    ), patch(
        "codegraphcontext_ext.commands.viz_dashboard.build_server",
        return_value=object(),
    ), patch(
        "codegraphcontext_ext.commands.viz_dashboard.serve_until_interrupted",
        return_value=None,
    ):
        activate_project.return_value.slug = "flask"

        result = runner.invoke(
            build_ext_app(),
            ["viz-dashboard", "--project", "flask", "--no-open", "--port", "0"],
        )

    assert result.exit_code == 0
    activate_project.assert_called_once_with("flask")
    payload = extract_last_json(result.output)
    assert payload["kind"] == "viz_dashboard_serving"
    assert payload["project"] == "flask"


def test_viz_dashboard_respects_falkordb_backend_for_project_store(monkeypatch, tmp_path):
    from codegraphcontext_ext.embeddings import runtime

    monkeypatch.setenv("DEFAULT_DATABASE", "falkordb")
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    monkeypatch.setattr(runtime, "is_falkordb_available", lambda: True)
    monkeypatch.setattr(runtime, "is_kuzudb_available", lambda: False)
    graph = {
        "nodes": [{"id": "u1", "name": "foo", "path": "a.py", "line": 1, "type": "Function"}],
        "edges": [],
    }

    with patch(
        "codegraphcontext_ext.commands.viz_dashboard.activate_project",
    ) as activate_project, patch(
        "codegraphcontext_ext.commands.viz_dashboard.get_kuzu_connection",
        return_value=object(),
    ), patch(
        "codegraphcontext_ext.commands.viz_dashboard._fetch_graph",
        return_value=graph,
    ), patch(
        "codegraphcontext_ext.commands.viz_dashboard.fetch_embedded_nodes",
        return_value=[],
    ), patch(
        "codegraphcontext_ext.commands.viz_dashboard._prepare_dashboard_serve_dir",
        return_value=tmp_path,
    ), patch(
        "codegraphcontext_ext.commands.viz_dashboard.find_free_port",
        return_value=43123,
    ), patch(
        "codegraphcontext_ext.commands.viz_dashboard.build_server",
        return_value=object(),
    ), patch(
        "codegraphcontext_ext.commands.viz_dashboard.serve_until_interrupted",
        return_value=None,
    ):
        activate_project.return_value.slug = "cgraph"

        result = runner.invoke(
            build_ext_app(),
            ["viz-dashboard", "--no-open", "--port", "0"],
        )

    assert result.exit_code == 0
    payload = extract_last_json(result.output)
    assert payload["kind"] == "viz_dashboard_serving"
    assert payload["project"] == "cgraph"
    assert os.environ.get("CGC_RUNTIME_DB_TYPE") is None


def test_capture_graph_screenshots_extracts_dashboard_url():
    line = 'cgraph dashboard: serving at http://127.0.0.1:3401/'
    assert (
        capture_graph_screenshots._extract_dashboard_url(line)
        == "http://127.0.0.1:3401/"
    )


def test_viz_dashboard_releases_kuzu_before_serving(monkeypatch, tmp_path):
    mark_kuzu_backend_available(monkeypatch)
    graph = {
        "nodes": [{"id": "u1", "name": "foo", "path": "a.py", "line": 1, "type": "Function"}],
        "edges": [],
    }
    close_calls: list[str] = []

    monkeypatch.setattr(
        viz_dashboard_mod,
        "_close_kuzu_connection",
        lambda: close_calls.append("closed"),
        raising=False,
    )

    with patch(
        "codegraphcontext_ext.commands.viz_dashboard.activate_project",
    ) as activate_project, patch(
        "codegraphcontext_ext.commands.viz_dashboard.get_kuzu_connection",
        return_value=object(),
    ), patch(
        "codegraphcontext_ext.commands.viz_dashboard._fetch_graph",
        return_value=graph,
    ), patch(
        "codegraphcontext_ext.commands.viz_dashboard.fetch_embedded_nodes",
        return_value=[],
    ), patch(
        "codegraphcontext_ext.commands.viz_dashboard._prepare_dashboard_serve_dir",
        return_value=tmp_path,
    ), patch(
        "codegraphcontext_ext.commands.viz_dashboard.find_free_port",
        return_value=43123,
    ), patch(
        "codegraphcontext_ext.commands.viz_dashboard.build_server",
        return_value=object(),
    ), patch(
        "codegraphcontext_ext.commands.viz_dashboard.serve_until_interrupted",
        return_value=None,
    ):
        activate_project.return_value.slug = "cgraph"

        result = runner.invoke(
            build_ext_app(),
            ["viz-dashboard", "--no-open", "--port", "0"],
        )

    assert result.exit_code == 0
    assert close_calls == ["closed"]


def test_dashboard_html_wires_primary_tabs_without_taxonomy():
    graph = {
        "nodes": [{"id": "u1", "name": "foo", "path": "a.py", "line": 1, "type": "Function"}],
        "edges": [],
    }
    html = _dashboard_html(graph, 0, "[]", "[]", layout="cose")

    # Primary tabs: 2D Graph, 3D Graph, Embeddings, and Standards.
    assert 'data-pane="pane-2d"' in html
    assert 'data-pane="pane-3d"' in html
    assert 'data-pane="pane-embeddings"' in html
    assert 'data-pane="pane-standards"' in html
    # Old scatter + separate Projector panes are gone.
    assert 'data-pane="pane-emb"' not in html
    assert 'data-pane="pane-projector"' not in html
    assert 'data-pane="pane-export"' not in html
    assert 'data-pane="pane-taxonomy"' not in html
    assert 'id="pane-taxonomy"' not in html
    assert ">Taxonomy</button>" not in html
    assert "data-tax-pane" not in html
    assert "window._taxInit" not in html
    assert "taxLoaded" not in html
    assert "__TAXONOMY_JSON__" not in html

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

    # About button (visible, not muted) and modal with purpose + credits.
    assert 'id="about-btn"' in html
    assert 'id="about-overlay"' in html
    assert "institutional knowledge" in html
    assert "Credits" in html
    assert "Cytoscape.js" in html


def test_dashboard_html_includes_clickable_stats_explainer():
    graph = {
        "nodes": [{"id": "u1", "name": "foo", "path": "a.py", "line": 1, "type": "Function"}],
        "edges": [],
    }
    count_details = {
        "full_node_counts": {"File": 10, "Module": 2, "Class": 4, "Function": 6, "Variable": 8},
        "full_node_total": 30,
        "full_edge_counts": {"CONTAINS": 11, "CALLS": 22, "IMPORTS": 33, "INHERITS": 44},
        "full_edge_total": 110,
        "embeddable_node_counts": {"Function": 6, "Class": 4},
        "embeddable_total": 10,
        "embedding_counts": {"Function": 5, "Class": 3},
        "stored_embedding_total": 8,
    }
    html = _dashboard_html(
        graph,
        8,
        "[]",
        "[]",
        layout="cose",
        graph_limit=500,
        count_details=count_details,
    )

    assert 'id="stats-panel"' in html
    assert 'id="stats-panel-close"' in html
    assert 'data-stat-target="graph-nodes"' in html
    assert 'data-stat-target="graph-edges"' in html
    assert 'data-stat-target="embeddings"' in html
    assert 'data-stat-card="graph-nodes"' in html
    assert 'data-stat-card="graph-edges"' in html
    assert 'data-stat-card="embeddings"' in html
    assert "These counts come from different slices of the project." in html
    assert "--limit 500" in html
    assert "Function and Class" in html
    assert "Full visualization-scope total: <code>30</code>" in html
    assert "Type breakdown: <code>File 10" in html
    assert "Relationship breakdown: <code>CONTAINS 11" in html
    assert "Embeddable symbols in graph scope: <code>10</code>" in html
    assert "Stored vector breakdown: <code>Function 5" in html
    assert "Coverage: <code>8 / 10 (80.0%)</code>" in html
    assert "not meant to be numerically identical" in html


def test_dashboard_html_stats_explainer_js_supports_toggle_and_focus():
    graph = {
        "nodes": [{"id": "u1", "name": "foo", "path": "a.py", "line": 1, "type": "Function"}],
        "edges": [],
    }
    html = _dashboard_html(graph, 7, "[]", "[]", layout="cose")

    assert 'const statButtons = document.querySelectorAll("[data-stat-target]");' in html
    assert "function openStatsPanel(target)" in html
    assert "function closeStatsPanel()" in html
    assert 'if (e.key === "Escape" && !statsPanel.hidden) closeStatsPanel();' in html


def test_collect_dashboard_count_details_reports_visualization_totals():
    class _CountConn:
        def execute(self, query, **_kwargs):
            rows = {
                "MATCH (n:`File`) RETURN count(n)": [(10,)],
                "MATCH (n:`Module`) RETURN count(n)": [(2,)],
                "MATCH (n:`Class`) RETURN count(n)": [(4,)],
                "MATCH (n:`Function`) RETURN count(n)": [(6,)],
                "MATCH (n:`Variable`) RETURN count(n)": [(8,)],
                "MATCH ()-[r:CONTAINS]->() RETURN count(r)": [(11,)],
                "MATCH ()-[r:CALLS]->() RETURN count(r)": [(22,)],
                "MATCH ()-[r:IMPORTS]->() RETURN count(r)": [(33,)],
                "MATCH ()-[r:INHERITS]->() RETURN count(r)": [(44,)],
                "MATCH (n:`Function`) WHERE n.`embedding` IS NOT NULL RETURN count(n)": [(5,)],
                "MATCH (n:`Class`) WHERE n.`embedding` IS NOT NULL RETURN count(n)": [(3,)],
            }
            return FakeResult(rows.get(query, []))

    details = _collect_dashboard_count_details(_CountConn())

    assert details["full_node_total"] == 30
    assert details["full_edge_total"] == 110
    assert details["embeddable_total"] == 10
    assert details["stored_embedding_total"] == 8
    assert details["full_node_counts"]["Variable"] == 8
    assert details["full_edge_counts"]["IMPORTS"] == 33
    assert details["embedding_counts"]["Class"] == 3


def test_dashboard_html_srcdoc_escapes_inner_html():
    """Inner HTMLs contain literal </script> and quotes; srcdoc must escape them."""
    graph = {
        "nodes": [{"id": "u1", "name": "</script>", "path": "a.py", "line": 1, "type": "Function"}],
        "edges": [],
    }
    html = _dashboard_html(graph, 0, "[]", "[]", layout="cose")
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

    with patch(
        "codegraphcontext_ext.commands.viz_dashboard.get_kuzu_connection",
        return_value=object(),
    ):
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


def test_standards_subtab_buttons_not_matched_by_outer_tab_selector():
    """Regression: clicking Configuration/Violations sub-tabs triggered the
    outer tab handler because both used class="tab".  The outer handler
    removed .active from all .pane elements (including pane-standards itself)
    and tried to activate a pane via data-pane — which sub-tabs don't have —
    resulting in a black screen.

    Fix: outer selector must use '#tab-bar .tab' (or '[data-pane]') so
    Standards sub-tab buttons are excluded.
    """
    graph = {
        "nodes": [{"id": "u1", "name": "foo", "path": "a.py", "line": 1, "type": "Function"}],
        "edges": [],
    }
    html = _dashboard_html(graph, 0, "[]", "[]", layout="cose")

    # The outer tab selector must NOT be querySelectorAll(".tab") — it must
    # be scoped to only match the 4 main nav tabs, not the standards sub-tabs.
    # Either '#tab-bar .tab' or '.tab-bar .tab' or '[data-pane]' is acceptable.
    assert 'querySelectorAll(".tab")' not in html, (
        "Outer tab selector must be scoped (e.g. '#tab-bar .tab') to avoid "
        "matching Standards sub-tab buttons that share the .tab class"
    )


def test_dashboard_html_violation_rows_use_resizable_fixed_width_table_contract():
    graph = {
        "nodes": [{"id": "u1", "name": "foo", "path": "a.py", "line": 1, "type": "Function"}],
        "edges": [],
    }
    standards_json = json.dumps([{
        "id": "cross_file_private_access",
        "category": "coupling",
        "severity": "warn",
        "summary": "Cross-file private access.",
        "suggestion": "Use a public API.",
        "evidence": "CALLS edge across files.",
        "thresholds": {},
    }])
    violations_json = json.dumps([{
        "standard_id": "cross_file_private_access",
        "severity": "warn",
        "kind": "cross_file_private_access",
        "offenders": [{
            "uid": "u1",
            "name": "foo",
            "path": "src/really/long/path/module_name.py",
            "line_number": 41,
            "metric_value": "_bar",
        }],
    }])

    html = _dashboard_html(graph, 0, standards_json, violations_json, layout="cose")

    assert "std-offender-table" in html
    assert "table-layout:fixed" in html
    assert "data-col-resizer" in html
    assert "data-viz-id" in html
    assert "setupOffenderTableResizers" in html


def test_dashboard_html_includes_stowable_violations_explainer():
    graph = {
        "nodes": [{"id": "u1", "name": "foo", "path": "a.py", "line": 1, "type": "Function"}],
        "edges": [],
    }
    standards_json = json.dumps([{
        "id": "cross_file_private_access",
        "category": "coupling",
        "severity": "warn",
        "summary": "Cross-file private access.",
        "suggestion": "Use a public API.",
        "evidence": "CALLS edge across files.",
        "thresholds": {},
    }])
    violations_json = json.dumps([{
        "standard_id": "cross_file_private_access",
        "severity": "warn",
        "kind": "cross_file_private_access",
        "offenders": [{
            "uid": "u1",
            "name": "foo",
            "path": "src/really/long/path/module_name.py",
            "line_number": 41,
            "metric_value": "_bar",
        }],
    }])

    html = _dashboard_html(graph, 0, standards_json, violations_json, layout="cose")

    assert 'id="std-viol-explainer"' in html
    assert 'id="std-viol-explainer-toggle"' in html
    assert 'aria-controls="std-viol-explainer-body"' in html
    assert "What this tab shows" in html
    assert "Severity dot" in html
    assert "2D button" in html
    assert "Configure this rule" in html


def test_dashboard_html_violation_details_include_rule_principle_copy():
    graph = {
        "nodes": [{"id": "u1", "name": "foo", "path": "a.py", "line": 1, "type": "Function"}],
        "edges": [],
    }
    standards_json = json.dumps([{
        "id": "cross_file_private_access",
        "category": "coupling",
        "severity": "warn",
        "summary": "Cross-file private access.",
        "suggestion": "Use a public API.",
        "evidence": "CALLS edge across files.",
        "principle": "Respect module boundaries and keep private implementation details private.",
        "thresholds": {},
    }])
    violations_json = json.dumps([{
        "standard_id": "cross_file_private_access",
        "severity": "warn",
        "kind": "cross_file_private_access",
        "offenders": [{
            "uid": "u1",
            "name": "foo",
            "path": "src/really/long/path/module_name.py",
            "line_number": 41,
            "metric_value": "_bar",
        }],
    }])

    html = _dashboard_html(graph, 0, standards_json, violations_json, layout="cose")

    assert "rule.principle" in html
    assert "Principle:" in html
    assert "Respect module boundaries and keep private implementation details private." in html


def test_load_standards_json_includes_principles_for_shipped_rules():
    data = json.loads(_load_standards_json())

    assert data, "expected shipped standards JSON"
    assert all(item.get("principle") for item in data), (
        "every shipped dashboard rule should expose a principle blurb"
    )

    cross_file_private_access = next(
        item for item in data if item["id"] == "cross_file_private_access"
    )
    assert cross_file_private_access["principle"] == (
        "Respect module boundaries and keep private implementation details private."
    )

# ── Loading animation overlay tests ──────────────────────────────


def _build_dashboard_html():
    graph = {
        "nodes": [{"id": "u1", "name": "foo", "path": "a.py", "line": 1, "type": "Function"}],
        "edges": [],
    }
    return _dashboard_html(graph, 0, "[]", "[]", layout="cose")


def test_dashboard_html_loading_overlays_present():
    """Every pane gets a loading overlay with canvas + label."""
    html = _build_dashboard_html()
    for pane_id in ("pane-2d", "pane-3d", "pane-embeddings", "pane-standards"):
        assert f'id="kkg-loader-{pane_id}"' in html, f"Missing loader for {pane_id}"
        assert f'id="kkg-loader-canvas-{pane_id}"' in html, f"Missing canvas for {pane_id}"


def test_dashboard_html_loading_css_injected():
    html = _build_dashboard_html()
    assert "kkg-loader" in html
    assert "kkg-pulse-text" in html
    assert ".kkg-loader.fade-out" in html


def test_dashboard_html_loading_js_injected():
    html = _build_dashboard_html()
    assert "buildGraph" in html, "Animation engine JS missing"
    assert "_kkgLoaded" in html, "Fade-out hook missing"
    assert "spawnParticles" in html, "Particle system missing"


def test_dashboard_html_no_unreplaced_loading_placeholders():
    html = _build_dashboard_html()
    for placeholder in (
        "__LOADING_CSS__", "__LOADING_JS__",
        "__LOADER_2D__", "__LOADER_3D__", "__LOADER_EMB__",
        "__LOADER_STD__",
    ):
        assert placeholder not in html, f"Unreplaced placeholder: {placeholder}"
