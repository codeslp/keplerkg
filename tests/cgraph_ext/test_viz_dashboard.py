"""Tests for kkg viz-dashboard — server-backed 4-tab dashboard.

The blocking serve_forever is not exercised here; tests verify the
deterministic pre-serve setup: dashboard HTML shape, srcdoc iframe
escaping, Projector tempdir layout, and the typed error paths.
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

from typer.testing import CliRunner

from codegraphcontext_ext.commands.viz_dashboard import (
    _annotate_taxonomy_profiles,
    _dashboard_html,
    _extract_rationale_comments,
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


class _RationaleConn:
    """Respond to per-symbol rationale lookups with fixed source/docstring rows."""

    def __init__(self, rows_by_uid):
        self._rows_by_uid = dict(rows_by_uid)

    def execute(self, _query, **kwargs):
        uid = kwargs.get("uid")
        row = self._rows_by_uid.get(uid)
        if row is None:
            return FakeResult([])
        return FakeResult([row])


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


def test_viz_dashboard_prefers_project_kuzu_over_global_default_database(monkeypatch, tmp_path):
    from codegraphcontext_ext.embeddings import runtime

    monkeypatch.setenv("DEFAULT_DATABASE", "falkordb")
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    monkeypatch.setattr(runtime, "is_kuzudb_available", lambda: True)
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


def test_dashboard_html_wires_three_tabs_including_embeddings():
    graph = {
        "nodes": [{"id": "u1", "name": "foo", "path": "a.py", "line": 1, "type": "Function"}],
        "edges": [],
    }
    html = _dashboard_html(graph, 0, "[]", "[]", layout="cose")

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

    # About button (visible, not muted) and modal with purpose + credits.
    assert 'id="about-btn"' in html
    assert 'id="about-overlay"' in html
    assert "institutional knowledge" in html
    assert "Credits" in html
    assert "Cytoscape.js" in html


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


# ---------------------------------------------------------------------------
# Taxonomy tab (Phase 5.5a)
# ---------------------------------------------------------------------------


def test_dashboard_html_wires_taxonomy_tab():
    graph = {
        "nodes": [{"id": "u1", "name": "foo", "path": "a.py", "line": 1, "type": "Function"}],
        "edges": [],
    }
    html = _dashboard_html(graph, 0, "[]", "[]", layout="cose")

    assert 'data-pane="pane-taxonomy"' in html
    assert 'id="pane-taxonomy"' in html
    assert ">Taxonomy</button>" in html


def test_dashboard_html_taxonomy_subtabs():
    graph = {
        "nodes": [{"id": "u1", "name": "foo", "path": "a.py", "line": 1, "type": "Function"}],
        "edges": [],
    }
    html = _dashboard_html(graph, 0, "[]", "[]", layout="cose")

    assert 'data-tax-pane="tax-structure"' in html
    assert 'data-tax-pane="tax-inheritance"' in html
    assert 'data-tax-pane="tax-communities"' in html
    assert ">Structure</button>" in html
    assert ">Inheritance</button>" in html
    assert ">Communities</button>" in html


def test_dashboard_html_includes_taxonomy_explainer_ribbon():
    graph = {
        "nodes": [{"id": "u1", "name": "foo", "path": "a.py", "line": 1, "type": "Function"}],
        "edges": [],
    }
    html = _dashboard_html(graph, 0, "[]", "[]", layout="cose")

    assert 'id="tax-explainer"' in html
    assert 'id="tax-explainer-toggle"' in html
    assert 'aria-controls="tax-explainer-body"' in html
    assert "Containment map." in html
    assert "Type hierarchy." in html
    assert "Semantic neighborhoods." in html


def test_dashboard_html_taxonomy_explainer_tracks_active_view():
    graph = {
        "nodes": [{"id": "u1", "name": "foo", "path": "a.py", "line": 1, "type": "Function"}],
        "edges": [],
    }
    html = _dashboard_html(graph, 0, "[]", "[]", layout="cose")

    assert "const TAX_EXPLAINER_COPY" in html
    assert "updateTaxonomyExplainer(btn.dataset.taxPane);" in html
    assert "updateTaxonomyExplainer('tax-structure');" in html
    assert 'data-tax-explainer-panel="tax-structure"' in html
    assert 'data-tax-explainer-panel="tax-inheritance"' in html
    assert 'data-tax-explainer-panel="tax-communities"' in html


def test_taxonomy_subtab_selectors_scoped():
    """Taxonomy sub-tabs use data-tax-pane, not data-pane or data-std-pane."""
    graph = {
        "nodes": [{"id": "u1", "name": "foo", "path": "a.py", "line": 1, "type": "Function"}],
        "edges": [],
    }
    html = _dashboard_html(graph, 0, "[]", "[]", layout="cose")

    # The taxonomy JS should use [data-tax-pane], not [data-pane]
    assert "data-tax-pane" in html
    # Lazy init should be present
    assert "window._taxInit" in html
    assert "taxLoaded" in html


def test_dashboard_html_taxonomy_data_injected():
    graph = {
        "nodes": [{"id": "u1", "name": "foo", "path": "a.py", "line": 1, "type": "Function"}],
        "edges": [],
    }
    tax = json.dumps({"structure": {"nodes": [], "stats": {}}, "inheritance": {"nodes": [], "edges": [], "roots": [], "stats": {}}, "communities": None})
    html = _dashboard_html(graph, 0, "[]", "[]", tax, layout="cose")

    # The taxonomy JSON should be injected (not the placeholder)
    assert "__TAXONOMY_JSON__" not in html
    assert '"communities":' in html


def test_extract_rationale_comments_reads_tagged_lines():
    notes = _extract_rationale_comments(
        """
        # WHY: keep the redirect local to preserve session affinity
        // HACK: remove after the OAuth migration finishes
        NOTE: shared cache key with the worker process
        TODO: ignore this line
        """
    )

    assert notes == [
        {"tag": "WHY", "text": "keep the redirect local to preserve session affinity"},
        {"tag": "HACK", "text": "remove after the OAuth migration finishes"},
        {"tag": "NOTE", "text": "shared cache key with the worker process"},
    ]


def test_annotate_taxonomy_profiles_adds_profile_cards_and_rationale():
    data = {
        "structure": {"nodes": [], "stats": {}},
        "inheritance": {"nodes": [], "edges": [], "roots": [], "stats": {}},
        "communities": {
            "communities": [
                {
                    "id": 0,
                    "size": 2,
                    "members": [
                        {
                            "uid": "u1",
                            "name": "login",
                            "path": "src/auth/routes.py",
                            "type": "Function",
                        },
                        {
                            "uid": "u2",
                            "name": "csrf_guard",
                            "path": "src/auth/routes.py",
                            "type": "Function",
                        },
                    ],
                }
            ],
            "edges": [],
            "cross_edges": [{"source_community": 0, "target_community": 1}],
            "stats": {"communities": 1},
        },
    }
    conn = _RationaleConn(
        {
            "u1": (
                "login",
                "src/auth/routes.py",
                21,
                "NOTE: public entry path",
                "# WHY: keep the auth flow explicit\n# HACK: remove after oauth v2 ships",
            ),
            "u2": (
                "csrf_guard",
                "src/auth/routes.py",
                44,
                "",
                "# NOTE: shared with legacy form posts",
            ),
        }
    )

    _annotate_taxonomy_profiles(data, conn)

    profile = data["communities"]["communities"][0]["profile"]
    assert profile["cross_edge_count"] == 1
    assert profile["dominant_types"][0] == {"type": "Function", "count": 2}
    assert profile["hotspots"][0] == {"path": "auth/routes.py", "count": 2}
    assert any(item["name"] == "csrf_guard" for item in profile["sample_members"])
    assert any(
        item["tag"] == "WHY" and item["symbol"] == "login"
        for item in profile["rationale"]
    )
    assert any(
        item["tag"] == "HACK" and "oauth v2" in item["text"]
        for item in profile["rationale"]
    )


def test_dashboard_html_includes_community_profile_rail():
    graph = {
        "nodes": [{"id": "u1", "name": "foo", "path": "a.py", "line": 1, "type": "Function"}],
        "edges": [],
    }
    tax = json.dumps(
        {
            "structure": {"nodes": [], "stats": {}},
            "inheritance": {"nodes": [], "edges": [], "roots": [], "stats": {}},
            "communities": {
                "communities": [],
                "edges": [],
                "cross_edges": [],
                "stats": {
                    "communities": 0,
                    "total_nodes": 0,
                    "total_edges": 0,
                    "structural_edges": 0,
                    "semantic_edges": 0,
                    "cross_community_edges": 0,
                },
            },
        }
    )

    html = _dashboard_html(graph, 0, "[]", "[]", tax, layout="cose")

    assert 'id="tax-comm-profiles"' in html
    assert "Community profiles" in html
    assert "Rationale comments (WHY/HACK/NOTE)" in html
    assert "data-community-card" in html


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
    for pane_id in ("pane-2d", "pane-3d", "pane-embeddings", "pane-standards", "pane-taxonomy"):
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
        "__LOADER_STD__", "__LOADER_TAX__",
    ):
        assert placeholder not in html, f"Unreplaced placeholder: {placeholder}"


def test_dashboard_html_taxonomy_loader_deferred_to_tab_activation():
    """Regression: taxonomy loader dismissal must be deferred to when
    _taxInit is actually called (i.e. user clicks the Taxonomy tab), not
    started eagerly at page load.

    _taxInit loads Cytoscape scripts asynchronously, so the observer +
    fallback timer must start inside the _taxInit wrapper, not at DOMReady.
    An eager 12s timeout would expire on a tab the user never opened."""
    html = _build_dashboard_html()
    import re
    # The _taxInit wrapper must exist and contain the MutationObserver logic
    # INSIDE the wrapper (deferred), not as a standalone IIFE at page load.
    assert "origTaxInit" in html, "Must wrap _taxInit to defer observer"
    assert "MutationObserver" in html, "Must use MutationObserver for render detection"
    assert "12000" in html, "Must have 12s fallback timeout"
    # The observer must be inside the _taxInit wrapper (deferred start).
    # Verify: the MutationObserver appears AFTER origTaxInit() call.
    pattern = r"origTaxInit\(\);\s*.*?MutationObserver"
    assert re.search(pattern, html, re.DOTALL), (
        "MutationObserver must be started inside _taxInit wrapper "
        "(deferred to tab activation), not at page load"
    )
    # _kkgLoaded("pane-taxonomy") should only appear inside the observer
    # callback and fallback — never as a direct call after origTaxInit().
    # Verify no immediate dismiss pattern like: origTaxInit(); _kkgLoaded(...)
    immediate_pattern = r"origTaxInit\(\);\s*(?:if\s*\(window\._kkgLoaded\))?\s*window\._kkgLoaded\(['\"]pane-taxonomy['\"]\)"
    assert not re.search(immediate_pattern, html), (
        "_kkgLoaded('pane-taxonomy') must not be called immediately "
        "after origTaxInit() — dismissal must be observer-based"
    )
