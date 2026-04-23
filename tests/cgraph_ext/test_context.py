"""Tests for the kkg context hybrid retrieval command."""

import json
from unittest.mock import patch

import typer
from typer.testing import CliRunner

from codegraphcontext_ext.cli import register_extensions
from codegraphcontext_ext.embeddings import runtime
from codegraphcontext_ext.hybrid.ann import search as ann_search, search_scoped
from codegraphcontext_ext.hybrid.traverse import traverse
from codegraphcontext_ext.commands.context import (
    _build_context_payload,
    _estimate_tokens,
    build_search_payload,
)

runner = CliRunner()


def _extract_json(output: str) -> dict:
    """Extract the JSON object from CLI output that may have stderr mixed in."""
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise ValueError(f"No JSON found in output: {output!r}")


def _context_app() -> typer.Typer:
    app = typer.Typer()

    @app.callback()
    def _root() -> None:
        return None

    register_extensions(app)
    return app


# --- Fake DB helpers ---


class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)
        self._idx = 0

    def has_next(self):
        return self._idx < len(self._rows)

    def get_next(self):
        row = self._rows[self._idx]
        self._idx += 1
        return row


class _FakeConn:
    """Mock KùzuDB connection that returns canned results per query pattern."""

    def __init__(self, ann_rows=None, traverse_rows=None, scan_rows=None, ann_error: bool = False):
        self._ann_rows = ann_rows or []
        self._traverse_rows = traverse_rows or []
        self._scan_rows = scan_rows or []
        self._ann_error = ann_error
        self.queries: list[str] = []

    def execute(self, query, *, parameters=None):
        self.queries.append(query)
        if "hnsw_search" in query.lower():
            if self._ann_error:
                raise RuntimeError("hnsw unavailable")
            return _FakeResult(self._ann_rows)
        if "AS embedding" in query and "embedding" in query:
            return _FakeResult(self._scan_rows)
        if "MATCH" in query:
            return _FakeResult(self._traverse_rows)
        return _FakeResult([])


# --- Unit tests: ann.search ---


def test_ann_search_returns_scored_seeds():
    ann_rows = [
        ("uid1", "verify_token", "src/auth.py", 42, 0.2),
        ("uid2", "hash_password", "src/auth.py", 78, 0.5),
    ]
    conn = _FakeConn(ann_rows=ann_rows)
    results = ann_search(conn, [0.1] * 768, k=8)

    # 2 rows per table × 2 tables (Function, Class) = 4, capped by k=8
    assert len(results) == 4
    # Closer distance = higher score; top results should be uid1 (distance 0.2)
    assert results[0]["score"] > results[2]["score"]
    assert results[0]["uid"] == "uid1"
    assert results[0]["file"] == "src/auth.py:42"


def test_ann_search_empty_db():
    conn = _FakeConn(ann_rows=[])
    results = ann_search(conn, [0.1] * 768, k=8)
    assert results == []


def test_ann_search_respects_k():
    rows = [
        (f"uid{i}", f"fn{i}", "src/a.py", i, float(i))
        for i in range(20)
    ]
    conn = _FakeConn(ann_rows=rows)
    results = ann_search(conn, [0.1] * 768, k=3)
    assert len(results) == 3


def test_ann_search_falls_back_to_linear_scan_when_hnsw_unavailable():
    scan_rows = [
        ("uid1", "request_ctx", "src/ctx.py", 10, [0.1, 0.1, 0.1]),
        ("uid2", "app_ctx", "src/ctx.py", 20, [0.9, 0.9, 0.9]),
    ]
    conn = _FakeConn(ann_error=True, scan_rows=scan_rows)

    results = ann_search(conn, [0.1, 0.1, 0.1], k=2, tables=("Function",))

    assert [row["uid"] for row in results] == ["uid1", "uid2"]
    assert results[0]["score"] > results[1]["score"]


# --- Unit tests: traverse ---


def test_traverse_returns_neighborhood():
    traverse_rows = [
        ("uid_caller", "call_me", "src/b.py", 10, "Function"),
    ]
    conn = _FakeConn(traverse_rows=traverse_rows)
    result = traverse(conn, ["uid1"], depth=1)

    assert "callers" in result
    assert "callees" in result
    assert "imports" in result


def test_traverse_deduplicates_across_categories():
    # Same node appears in both callers and callees queries
    traverse_rows = [
        ("uid_shared", "shared_fn", "src/c.py", 5, "Function"),
    ]
    conn = _FakeConn(traverse_rows=traverse_rows)
    result = traverse(conn, ["uid1"], depth=1)

    # uid_shared should appear in callers (first category processed)
    # but not again in callees or imports
    all_uids = (
        [n["uid"] for n in result["callers"]]
        + [n["uid"] for n in result["callees"]]
        + [n["uid"] for n in result["imports"]]
    )
    assert all_uids.count("uid_shared") == 1


def test_traverse_excludes_seed_uids():
    traverse_rows = [
        ("uid1", "seed_fn", "src/a.py", 1, "Function"),  # This is a seed
        ("uid_other", "other_fn", "src/b.py", 2, "Function"),
    ]
    conn = _FakeConn(traverse_rows=traverse_rows)
    result = traverse(conn, ["uid1"], depth=1)

    all_uids = (
        [n["uid"] for n in result["callers"]]
        + [n["uid"] for n in result["callees"]]
        + [n["uid"] for n in result["imports"]]
    )
    assert "uid1" not in all_uids
    assert "uid_other" in all_uids


def test_traverse_empty_seeds():
    conn = _FakeConn()
    result = traverse(conn, [], depth=1)
    assert result == {"callers": [], "callees": [], "imports": []}


# --- Regression: FalkorDB shim translates Kuzu-only label() projections ---


def test_falkordb_shim_translates_label_to_labels_on_traverse():
    """Kuzu-style ``label(target) AS kind`` must become ``labels(target)[0]``
    when routed through the FalkorDB compat shim, so Falkor-backed traverse
    returns populated kinds instead of silently empty neighborhoods.
    """
    from codegraphcontext.core.database_falkordb import FalkorDBKuzuCompatConnection

    class _FakeFalkorResult:
        def __init__(self, rows):
            self.result_set = rows

    class _FakeFalkorGraph:
        def __init__(self):
            self.queries: list[str] = []

        def query(self, query, params):
            self.queries.append(query)
            if "labels(target)" in query or "labels(source)" in query:
                return _FakeFalkorResult(
                    [["uid_caller", "call_me", "src/b.py", 10, "Function"]]
                )
            return _FakeFalkorResult([])

    graph = _FakeFalkorGraph()
    conn = FalkorDBKuzuCompatConnection(graph)
    result = traverse(conn, ["uid_seed"], depth=1)

    assert not any("label(target)" in q for q in graph.queries), (
        f"Kuzu-only label() syntax leaked into FalkorDB: {graph.queries!r}"
    )
    assert not any("label(source)" in q for q in graph.queries), (
        f"Kuzu-only label() syntax leaked into FalkorDB: {graph.queries!r}"
    )
    assert any("labels(target)[0]" in q for q in graph.queries)
    assert any("labels(source)[0]" in q for q in graph.queries)

    returned_kinds = {
        node["kind"]
        for bucket in ("callers", "callees", "imports")
        for node in result[bucket]
    }
    assert returned_kinds == {"Function"}


def test_translate_kuzu_read_query_rewrites_label_and_leaves_rest():
    from codegraphcontext.core.database_falkordb import _translate_kuzu_read_query

    assert _translate_kuzu_read_query(
        "MATCH (n) RETURN label(n) AS kind"
    ) == "MATCH (n) RETURN labels(n)[0] AS kind"

    assert _translate_kuzu_read_query(
        "MATCH (n) RETURN labels(n) AS kinds"
    ) == "MATCH (n) RETURN labels(n) AS kinds"

    assert _translate_kuzu_read_query(
        "MATCH (a)-[r]->(b) RETURN label(a) AS ka, label(b) AS kb"
    ) == "MATCH (a)-[r]->(b) RETURN labels(a)[0] AS ka, labels(b)[0] AS kb"


# --- Unit tests: payload builder ---


def test_build_context_payload_shape():
    seeds = [{"uid": "uid1", "name": "foo", "file": "a.py:1", "table": "Function", "score": 0.9}]
    neighborhood = {"callers": [], "callees": [], "imports": []}
    payload = _build_context_payload("auth flow", seeds, neighborhood)

    assert payload["query"] == "auth flow"
    assert payload["seeds"] == seeds
    assert payload["neighborhood"] == neighborhood
    assert isinstance(payload["token_estimate"], int)
    assert payload["token_estimate"] > 0
    assert "cl100k_base" in payload["token_estimate_basis"]


def test_estimate_tokens_is_positive():
    assert _estimate_tokens("hello world") > 0
    assert _estimate_tokens("") == 1  # min 1


# --- End-to-end command tests ---


class _MockProvider:
    def __init__(self, dims=768):
        self._dims = dims

    @property
    def dimensions(self):
        return self._dims

    def embed_texts(self, texts):
        return [[0.1] * self._dims for _ in texts]


def test_context_command_emits_json(monkeypatch):
    monkeypatch.setenv("DEFAULT_DATABASE", "kuzudb")
    monkeypatch.setattr(runtime, "is_kuzudb_available", lambda: True)

    fake_conn = _FakeConn(ann_rows=[], traverse_rows=[])

    with patch(
        "codegraphcontext_ext.commands.context.get_kuzu_connection",
        return_value=fake_conn,
    ), patch(
        "codegraphcontext_ext.commands.context.create_provider",
        return_value=_MockProvider(),
    ):
        app = _context_app()
        result = runner.invoke(app, ["search", "auth flow"])

    assert result.exit_code == 0
    payload = _extract_json(result.output)
    assert payload["query"] == "auth flow"
    assert payload["seeds"] == []
    assert payload["neighborhood"] == {"callers": [], "callees": [], "imports": []}
    assert "token_estimate" in payload


def test_context_command_with_seeds(monkeypatch):
    monkeypatch.setenv("DEFAULT_DATABASE", "kuzudb")
    monkeypatch.setattr(runtime, "is_kuzudb_available", lambda: True)

    ann_rows = [
        ("uid1", "verify_token", "src/auth.py", 42, 0.2),
    ]
    traverse_rows = [
        ("uid_caller", "main_handler", "src/api.py", 10, "Function"),
    ]
    fake_conn = _FakeConn(ann_rows=ann_rows, traverse_rows=traverse_rows)

    with patch(
        "codegraphcontext_ext.commands.context.get_kuzu_connection",
        return_value=fake_conn,
    ), patch(
        "codegraphcontext_ext.commands.context.create_provider",
        return_value=_MockProvider(),
    ):
        app = _context_app()
        result = runner.invoke(app, ["search", "auth flow", "--k", "4", "--depth", "2"])

    assert result.exit_code == 0
    payload = _extract_json(result.output)
    assert len(payload["seeds"]) > 0
    assert payload["seeds"][0]["name"] == "verify_token"


def test_build_search_payload_with_seeds(monkeypatch):
    monkeypatch.setenv("DEFAULT_DATABASE", "kuzudb")
    monkeypatch.setattr(runtime, "is_kuzudb_available", lambda: True)

    ann_rows = [
        ("uid1", "verify_token", "src/auth.py", 42, 0.2),
    ]
    traverse_rows = [
        ("uid_caller", "main_handler", "src/api.py", 10, "Function"),
    ]
    fake_conn = _FakeConn(ann_rows=ann_rows, traverse_rows=traverse_rows)

    with patch(
        "codegraphcontext_ext.commands.context.get_kuzu_connection",
        return_value=fake_conn,
    ), patch(
        "codegraphcontext_ext.commands.context.create_provider",
        return_value=_MockProvider(),
    ):
        payload = build_search_payload("auth flow", k=4, depth=2)

    assert payload["query"] == "auth flow"
    assert len(payload["seeds"]) > 0
    assert payload["seeds"][0]["name"] == "verify_token"
    assert payload["neighborhood"]["callers"][0]["uid"] == "uid_caller"


def test_context_command_rejects_unsupported_backend(monkeypatch):
    """Spec 006 admits FalkorDB; genuinely unsupported backends (neo4j) still fail."""
    monkeypatch.setenv("DEFAULT_DATABASE", "neo4j")
    monkeypatch.setattr(
        "codegraphcontext_ext.embeddings.runtime.is_falkordb_remote_configured",
        lambda: False,
    )
    monkeypatch.setattr(
        "codegraphcontext_ext.embeddings.runtime.is_falkordb_available",
        lambda: False,
    )
    monkeypatch.setattr(
        "codegraphcontext_ext.embeddings.runtime.is_kuzudb_available",
        lambda: False,
    )
    monkeypatch.setattr(
        "codegraphcontext_ext.embeddings.runtime.is_neo4j_configured",
        lambda: True,
    )
    app = _context_app()
    result = runner.invoke(app, ["search", "auth flow"])

    assert result.exit_code == 1
    payload = _extract_json(result.output)
    assert payload["kind"] == "unsupported_backend"
    assert payload["backend"] == "neo4j"


# --- search_scoped tests ---


def test_search_scoped_filters_by_allowed_uids():
    # scan_rows format: (uid, name, path, line_number, embedding_vector)
    scan_rows = [
        ("uid1", "fn_a", "a.py", 1, [0.1, 0.1, 0.1]),
        ("uid2", "fn_b", "b.py", 2, [0.5, 0.5, 0.5]),
        ("uid3", "fn_c", "c.py", 3, [0.1, 0.1, 0.2]),
    ]
    conn = _FakeConn(scan_rows=scan_rows)
    results = search_scoped(
        conn, [0.1, 0.1, 0.1], k=8,
        allowed_uids={"uid1", "uid3"},
        tables=("Function",),
    )
    result_uids = {r["uid"] for r in results}
    assert "uid2" not in result_uids
    assert result_uids == {"uid1", "uid3"}


def test_search_scoped_empty_allowed():
    scan_rows = [("uid1", "fn", "a.py", 1, [0.1, 0.1])]
    conn = _FakeConn(scan_rows=scan_rows)
    results = search_scoped(conn, [0.1, 0.1], k=8, allowed_uids=set())
    assert results == []


def test_search_scoped_respects_k():
    scan_rows = [
        (f"uid{i}", f"fn{i}", "a.py", i, [float(i) * 0.1, 0.0])
        for i in range(20)
    ]
    conn = _FakeConn(scan_rows=scan_rows)
    allowed = {f"uid{i}" for i in range(20)}
    results = search_scoped(
        conn, [0.1, 0.0], k=3,
        allowed_uids=allowed,
        tables=("Function",),
    )
    assert len(results) <= 3


def test_search_scoped_finds_hit_beyond_global_topn():
    """Regression: the best in-scope hit may be globally ranked beyond any
    inflated-k window.  search_scoped must still find it because it scans
    the allowed set directly rather than post-filtering global results."""
    # 50 out-of-scope nodes very close to query vector
    scan_rows = [
        (f"noise{i}", f"noise_fn{i}", "noise.py", i, [0.1, 0.1, 0.1])
        for i in range(50)
    ]
    # One in-scope node — also close to query but would be ranked 51st
    # if we only post-filtered a global top-50.
    scan_rows.append(("target", "target_fn", "target.py", 1, [0.1, 0.1, 0.1]))

    conn = _FakeConn(scan_rows=scan_rows)
    results = search_scoped(
        conn, [0.1, 0.1, 0.1], k=3,
        allowed_uids={"target"},
        tables=("Function",),
    )
    assert len(results) == 1
    assert results[0]["uid"] == "target"


# --- CLI mode tests ---


def test_context_command_with_cluster_mode(monkeypatch):
    monkeypatch.setenv("DEFAULT_DATABASE", "kuzudb")
    monkeypatch.setattr(runtime, "is_kuzudb_available", lambda: True)

    # search_scoped uses _linear_scan_scoped which queries embeddings (scan_rows)
    scan_rows = [
        ("uid1", "fn_a", "a.py", 1, [0.1] * 768),
        ("uid2", "fn_b", "b.py", 2, [0.9] * 768),
    ]
    fake_conn = _FakeConn(scan_rows=scan_rows)

    with patch(
        "codegraphcontext_ext.commands.context.get_kuzu_connection",
        return_value=fake_conn,
    ), patch(
        "codegraphcontext_ext.commands.context.create_provider",
        return_value=_MockProvider(),
    ), patch(
        "codegraphcontext_ext.commands.context._resolve_cluster_uids",
        return_value={"uid1"},
    ):
        app = _context_app()
        result = runner.invoke(app, [
            "search", "auth flow", "--mode", "cluster", "--cluster-id", "0",
        ])

    assert result.exit_code == 0
    payload = _extract_json(result.output)
    seed_uids = {s["uid"] for s in payload["seeds"]}
    # uid1 must be present (in allowed set), uid2 must be excluded
    assert "uid1" in seed_uids
    assert "uid2" not in seed_uids


def test_context_command_with_community_mode(monkeypatch):
    monkeypatch.setenv("DEFAULT_DATABASE", "kuzudb")
    monkeypatch.setattr(runtime, "is_kuzudb_available", lambda: True)

    scan_rows = [
        ("uid1", "fn_a", "a.py", 1, [0.9] * 768),
        ("uid2", "fn_b", "b.py", 2, [0.1] * 768),
    ]
    fake_conn = _FakeConn(scan_rows=scan_rows)

    with patch(
        "codegraphcontext_ext.commands.context.get_kuzu_connection",
        return_value=fake_conn,
    ), patch(
        "codegraphcontext_ext.commands.context.create_provider",
        return_value=_MockProvider(),
    ), patch(
        "codegraphcontext_ext.commands.context._resolve_community_uids",
        return_value={"uid2"},
    ):
        app = _context_app()
        result = runner.invoke(app, [
            "search", "auth flow", "--mode", "community", "--community-id", "0",
        ])

    assert result.exit_code == 0
    payload = _extract_json(result.output)
    seed_uids = {s["uid"] for s in payload["seeds"]}
    # uid2 must be present (in allowed set), uid1 must be excluded
    assert "uid2" in seed_uids
    assert "uid1" not in seed_uids


def test_context_command_rejects_invalid_mode(monkeypatch):
    monkeypatch.setenv("DEFAULT_DATABASE", "kuzudb")
    app = _context_app()
    result = runner.invoke(app, ["search", "test", "--mode", "bogus"])
    assert result.exit_code != 0


def test_context_command_rejects_cluster_mode_without_id(monkeypatch):
    monkeypatch.setenv("DEFAULT_DATABASE", "kuzudb")
    app = _context_app()
    result = runner.invoke(app, ["search", "test", "--mode", "cluster"])
    assert result.exit_code != 0


def test_context_command_rejects_community_mode_without_id(monkeypatch):
    monkeypatch.setenv("DEFAULT_DATABASE", "kuzudb")
    app = _context_app()
    result = runner.invoke(app, ["search", "test", "--mode", "community"])
    assert result.exit_code != 0


def test_context_command_rejects_global_with_community_id(monkeypatch):
    monkeypatch.setenv("DEFAULT_DATABASE", "kuzudb")
    app = _context_app()
    result = runner.invoke(app, ["search", "test", "--mode", "global", "--community-id", "0"])
    assert result.exit_code != 0


def test_context_command_rejects_global_with_cluster_id(monkeypatch):
    monkeypatch.setenv("DEFAULT_DATABASE", "kuzudb")
    app = _context_app()
    result = runner.invoke(app, ["search", "test", "--mode", "global", "--cluster-id", "0"])
    assert result.exit_code != 0


def test_context_command_rejects_cluster_with_community_id(monkeypatch):
    monkeypatch.setenv("DEFAULT_DATABASE", "kuzudb")
    app = _context_app()
    result = runner.invoke(app, ["search", "test", "--mode", "cluster", "--cluster-id", "0", "--community-id", "1"])
    assert result.exit_code != 0


def test_context_command_rejects_community_with_cluster_id(monkeypatch):
    monkeypatch.setenv("DEFAULT_DATABASE", "kuzudb")
    app = _context_app()
    result = runner.invoke(app, ["search", "test", "--mode", "community", "--community-id", "0", "--cluster-id", "1"])
    assert result.exit_code != 0


def test_context_command_registered():
    app = _context_app()
    # Verify 'context' appears in the registered commands
    command_names = [cmd.name for cmd in app.registered_commands]
    assert "search" in command_names
