"""Tests for the kkg context hybrid retrieval command."""

import json
from unittest.mock import patch

import typer
from typer.testing import CliRunner

from codegraphcontext_ext.cli import register_extensions
from codegraphcontext_ext.embeddings import runtime
from codegraphcontext_ext.hybrid.ann import search as ann_search
from codegraphcontext_ext.hybrid.traverse import traverse
from codegraphcontext_ext.commands.context import (
    _build_context_payload,
    _estimate_tokens,
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

    def __init__(self, ann_rows=None, traverse_rows=None):
        self._ann_rows = ann_rows or []
        self._traverse_rows = traverse_rows or []
        self.queries: list[str] = []

    def execute(self, query, *, parameters=None):
        self.queries.append(query)
        if "hnsw_search" in query.lower():
            return _FakeResult(self._ann_rows)
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
        result = runner.invoke(app, ["context", "auth flow"])

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
        result = runner.invoke(app, ["context", "auth flow", "--k", "4", "--depth", "2"])

    assert result.exit_code == 0
    payload = _extract_json(result.output)
    assert len(payload["seeds"]) > 0
    assert payload["seeds"][0]["name"] == "verify_token"


def test_context_command_rejects_non_kuzu(monkeypatch):
    monkeypatch.setenv("DEFAULT_DATABASE", "falkordb")
    app = _context_app()
    result = runner.invoke(app, ["context", "auth flow"])

    assert result.exit_code == 1
    payload = _extract_json(result.output)
    assert payload["kind"] == "unsupported_backend"


def test_context_command_registered():
    app = _context_app()
    # Verify 'context' appears in the registered commands
    command_names = [cmd.name for cmd in app.registered_commands]
    assert "context" in command_names
