import json
from unittest.mock import MagicMock, patch, call

import typer
from typer.testing import CliRunner

from codegraphcontext_ext.cli import register_extensions
from codegraphcontext_ext.embeddings import runtime
from codegraphcontext_ext.embeddings.schema import (
    EMBEDDABLE_TABLES,
    EMBEDDING_COLUMN,
    ensure_embedding_columns,
    ensure_hnsw_indexes,
)
from codegraphcontext_ext.embeddings.providers import (
    LocalProvider,
    create_provider,
)
from codegraphcontext_ext.commands.embed import (
    _build_embed_text,
    _fetch_nodes,
    _run_name_embed,
    _write_embeddings,
    _run_embed,
)

runner = CliRunner()


def _extract_json(output: str) -> dict:
    """Extract the JSON object from CLI output that may have stderr mixed in."""
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise ValueError(f"No JSON found in output: {output!r}")


def _embed_app() -> typer.Typer:
    app = typer.Typer()

    @app.callback()
    def _root() -> None:
        return None

    register_extensions(app)
    return app


def test_resolve_requested_backend_prefers_explicit_env(monkeypatch):
    monkeypatch.setenv("DEFAULT_DATABASE", "neo4j")
    monkeypatch.setattr(runtime, "is_falkordb_remote_configured", lambda: False)
    monkeypatch.setattr(runtime, "is_falkordb_available", lambda: True)
    monkeypatch.setattr(runtime, "is_kuzudb_available", lambda: True)
    monkeypatch.setattr(runtime, "is_neo4j_configured", lambda: True)

    assert runtime.resolve_requested_backend() == "neo4j"


def test_embed_check_model_accepts_falkordb_backend(monkeypatch):
    """Spec 006: FalkorDB is now a first-class embedding backend alongside Kuzu."""
    monkeypatch.setenv("DEFAULT_DATABASE", "falkordb")
    monkeypatch.setattr(runtime, "is_falkordb_available", lambda: True)
    monkeypatch.setattr(runtime, "has_local_embedding_runtime", lambda: True)
    app = _embed_app()

    result = runner.invoke(app, ["embed", "--check-model"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["backend"] == "falkordb"
    assert payload["kind"] == "ready"


def test_embed_check_model_rejects_unsupported_backend(monkeypatch):
    """Non-kuzu and non-falkordb backends (e.g. neo4j) are still rejected."""
    monkeypatch.setenv("DEFAULT_DATABASE", "neo4j")
    monkeypatch.setattr(runtime, "is_falkordb_remote_configured", lambda: False)
    monkeypatch.setattr(runtime, "is_falkordb_available", lambda: False)
    monkeypatch.setattr(runtime, "is_kuzudb_available", lambda: False)
    monkeypatch.setattr(runtime, "is_neo4j_configured", lambda: True)
    app = _embed_app()

    result = runner.invoke(app, ["embed", "--check-model"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["kind"] == "unsupported_backend"
    assert payload["backend"] == "neo4j"


def test_embed_check_model_reports_missing_falkordb_dependency(monkeypatch):
    monkeypatch.setenv("DEFAULT_DATABASE", "falkordb")
    monkeypatch.setattr(runtime, "is_falkordb_available", lambda: False)
    app = _embed_app()

    result = runner.invoke(app, ["embed", "--check-model"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["kind"] == "missing_backend_dependency"
    assert payload["backend"] == "falkordb"
    assert "falkordblite" in payload["detail"]


def test_embed_check_model_reports_missing_local_runtime(monkeypatch):
    monkeypatch.setenv("DEFAULT_DATABASE", "kuzudb")
    monkeypatch.setattr(runtime, "is_kuzudb_available", lambda: True)
    monkeypatch.setattr(runtime, "has_local_embedding_runtime", lambda: False)
    app = _embed_app()

    result = runner.invoke(app, ["embed", "--check-model"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["kind"] == "missing_dependency"
    assert payload["provider"] == "local"
    assert payload["model"] == "jinaai/jina-embeddings-v2-base-code"


def test_embed_check_model_succeeds_with_local_runtime(monkeypatch):
    monkeypatch.setenv("DEFAULT_DATABASE", "kuzudb")
    monkeypatch.setattr(runtime, "is_kuzudb_available", lambda: True)
    monkeypatch.setattr(runtime, "has_local_embedding_runtime", lambda: True)
    app = _embed_app()

    result = runner.invoke(app, ["embed", "--check-model"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["kind"] == "ready"
    assert payload["backend"] == "kuzudb"
    assert payload["provider"] == "local"


def test_embed_check_model_supports_openai_provider(monkeypatch):
    monkeypatch.setenv("DEFAULT_DATABASE", "kuzudb")
    monkeypatch.setattr(runtime, "is_kuzudb_available", lambda: True)
    monkeypatch.setattr(runtime, "has_openai_api_key", lambda: True)
    app = _embed_app()

    result = runner.invoke(app, ["embed", "--check-model", "--provider", "openai"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["provider"] == "openai"
    assert payload["model"] == "text-embedding-3-large"
    assert payload["kind"] == "ready"


def test_embed_without_check_model_runs_write_path(monkeypatch):
    """Without --check-model, embed attempts the write path (needs mocked DB)."""
    monkeypatch.setenv("DEFAULT_DATABASE", "kuzudb")
    monkeypatch.setattr(runtime, "is_kuzudb_available", lambda: True)

    fake_conn = _FakeConn()
    fake_conn.execute = lambda q, **kw: _FakeResult([])

    with patch(
        "codegraphcontext_ext.commands.embed.get_kuzu_connection",
        return_value=fake_conn,
    ), patch(
        "codegraphcontext_ext.commands.embed.ensure_embedding_columns",
        return_value=[],
    ), patch(
        "codegraphcontext_ext.commands.embed.ensure_hnsw_indexes",
        return_value=[],
    ):
        app = _embed_app()
        result = runner.invoke(app, ["embed"])

    assert result.exit_code == 0
    payload = _extract_json(result.output)
    assert payload["kind"] == "embed_complete"
    assert payload["backend"] == "kuzudb"


def test_runtime_db_type_wins_over_default_database(monkeypatch):
    monkeypatch.setenv("CGC_RUNTIME_DB_TYPE", "kuzudb")
    monkeypatch.setenv("DEFAULT_DATABASE", "neo4j")
    monkeypatch.setattr(runtime, "is_falkordb_remote_configured", lambda: False)
    monkeypatch.setattr(runtime, "is_falkordb_available", lambda: False)
    monkeypatch.setattr(runtime, "is_kuzudb_available", lambda: True)
    monkeypatch.setattr(runtime, "is_neo4j_configured", lambda: False)

    assert runtime.resolve_requested_backend() == "kuzudb"


def test_embed_check_model_reports_missing_voyage_api_key(monkeypatch):
    monkeypatch.setenv("DEFAULT_DATABASE", "kuzudb")
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.setattr(runtime, "is_kuzudb_available", lambda: True)
    monkeypatch.setattr(runtime, "has_voyage_api_key", lambda: False)
    app = _embed_app()

    result = runner.invoke(app, ["embed", "--check-model", "--provider", "voyage"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["kind"] == "missing_api_key"
    assert payload["provider"] == "voyage"
    assert payload["model"] == "voyage-code-3"
    assert "VOYAGE_API_KEY" in payload["detail"]


def test_embed_check_model_succeeds_with_voyage_api_key(monkeypatch):
    monkeypatch.setenv("DEFAULT_DATABASE", "kuzudb")
    monkeypatch.setattr(runtime, "is_kuzudb_available", lambda: True)
    monkeypatch.setattr(runtime, "has_voyage_api_key", lambda: True)
    app = _embed_app()

    result = runner.invoke(app, ["embed", "--check-model", "--provider", "voyage"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["kind"] == "ready"
    assert payload["provider"] == "voyage"
    assert payload["dimensions"] == 1024


def test_embed_rejects_unknown_provider_as_bad_parameter(monkeypatch):
    monkeypatch.setenv("DEFAULT_DATABASE", "kuzudb")
    monkeypatch.setattr(runtime, "is_kuzudb_available", lambda: True)
    app = _embed_app()

    result = runner.invoke(app, ["embed", "--check-model", "--provider", "cohere"])

    assert result.exit_code != 0
    import re
    clean = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    normalized = " ".join(clean.replace("│", " ").split())
    assert "Unsupported embedding provider 'cohere'" in normalized
    assert "local, voyage, openai" in normalized


# --- Schema mutation tests ---


class _FakeConn:
    """Mock KùzuDB connection that tracks executed queries."""

    def __init__(self, *, fail_alter=False, fail_index=False):
        self.executed: list[str] = []
        self._fail_alter = fail_alter
        self._fail_index = fail_index

    def execute(self, query, *, parameters=None):
        self.executed.append(query)
        if self._fail_alter and "ALTER TABLE" in query:
            raise Exception("already exists in table")
        if self._fail_index and "CREATE HNSW INDEX" in query:
            raise Exception("already exists")
        return _FakeResult([])


class _FakeResult:
    """Mock KùzuDB query result."""

    def __init__(self, rows):
        self._rows = list(rows)
        self._idx = 0

    def has_next(self):
        return self._idx < len(self._rows)

    def get_next(self):
        row = self._rows[self._idx]
        self._idx += 1
        return row


def test_ensure_embedding_columns_creates_columns():
    conn = _FakeConn()
    results = ensure_embedding_columns(conn, 768)

    assert len(results) == len(EMBEDDABLE_TABLES)
    for r in results:
        assert r["action"] == "created"
        assert "FLOAT[768]" in r["detail"]

    for table in EMBEDDABLE_TABLES:
        assert any(
            f"ALTER TABLE `{table}`" in q and "FLOAT[768]" in q
            for q in conn.executed
        )


def test_ensure_embedding_columns_idempotent():
    conn = _FakeConn(fail_alter=True)
    results = ensure_embedding_columns(conn, 768)

    for r in results:
        assert r["action"] == "exists"


def test_ensure_hnsw_indexes_creates_indexes():
    conn = _FakeConn()
    results = ensure_hnsw_indexes(conn, 768)

    assert len(results) == len(EMBEDDABLE_TABLES)
    for r in results:
        assert r["action"] == "created"
        assert "HNSW" in r["detail"] or "hnsw" in r["detail"]


def test_ensure_hnsw_indexes_idempotent():
    conn = _FakeConn(fail_index=True)
    results = ensure_hnsw_indexes(conn, 768)

    for r in results:
        assert r["action"] == "exists"


def test_ensure_embedding_columns_skips_on_falkordb(monkeypatch):
    """FalkorDB is schemaless — no ALTER TABLE runs; column materializes on SET."""
    monkeypatch.setenv("DEFAULT_DATABASE", "falkordb")
    conn = _FakeConn()

    results = ensure_embedding_columns(conn, 768)

    assert len(results) == len(EMBEDDABLE_TABLES)
    for r in results:
        assert r["action"] == "skipped_on_backend"
        assert "schemaless" in r["detail"].lower()
    assert conn.executed == [], "no DDL should be emitted on FalkorDB"


def test_ensure_hnsw_indexes_skips_on_falkordb(monkeypatch):
    """HNSW is Kuzu-specific — FalkorDB falls back to linear-scan ANN."""
    monkeypatch.setenv("DEFAULT_DATABASE", "falkordb")
    conn = _FakeConn()

    results = ensure_hnsw_indexes(conn, 768)

    assert len(results) == len(EMBEDDABLE_TABLES)
    for r in results:
        assert r["action"] == "skipped_on_backend"
        assert "linear-scan" in r["detail"]
    assert conn.executed == [], "no HNSW DDL should be emitted on FalkorDB"


# --- Provider factory tests ---


def test_create_provider_local():
    from codegraphcontext_ext.embeddings.runtime import EmbeddingConfig

    config = EmbeddingConfig(provider="local", model="test-model", dimensions=768)
    p = create_provider(config)
    assert isinstance(p, LocalProvider)
    assert p.dimensions == 768


def test_create_provider_unknown_raises():
    from codegraphcontext_ext.embeddings.runtime import EmbeddingConfig
    import pytest

    config = EmbeddingConfig(provider="unknown", model="x", dimensions=1)
    with pytest.raises(ValueError, match="No provider class"):
        create_provider(config)


# --- Build embed text tests ---


def test_build_embed_text_combines_fields():
    node = {"name": "foo", "docstring": "Does foo things", "source": "def foo(): pass"}
    text = _build_embed_text(node)
    assert "foo" in text
    assert "Does foo things" in text
    assert "def foo(): pass" in text


def test_build_embed_text_handles_missing_fields():
    node = {"name": "bar", "docstring": None, "source": None}
    text = _build_embed_text(node)
    assert text == "bar"


def test_build_embed_text_empty_node():
    node = {"name": None, "docstring": None, "source": None}
    text = _build_embed_text(node)
    assert text == ""


# --- Write path integration tests (mocked DB + provider) ---


class _MockProvider:
    """Deterministic mock embedding provider."""

    def __init__(self, dims=4):
        self._dims = dims
        self.calls: list[list[str]] = []

    @property
    def dimensions(self):
        return self._dims

    def embed_texts(self, texts):
        self.calls.append(texts)
        return [[0.1] * self._dims for _ in texts]


class _QueryableConn:
    """Mock connection that returns canned query results and tracks writes."""

    def __init__(self, node_rows):
        self._node_rows = list(node_rows)
        self._returned = False
        self.writes: list[dict] = []

    def execute(self, query, *, parameters=None):
        if "MATCH" in query and "SET" in query:
            self.writes.append({"query": query, "params": parameters})
            return _FakeResult([])
        if "MATCH" in query and "RETURN" in query:
            if not self._returned:
                self._returned = True
                return _FakeResult(self._node_rows)
            return _FakeResult([])
        return _FakeResult([])


def test_write_embeddings_writes_per_uid():
    conn = _QueryableConn([])
    uids = ["uid1", "uid2"]
    vecs = [[0.1, 0.2], [0.3, 0.4]]

    count = _write_embeddings(conn, "Function", uids, vecs)

    assert count == 2
    assert len(conn.writes) == 2
    assert conn.writes[0]["params"]["uid"] == "uid1"
    assert conn.writes[1]["params"]["uid"] == "uid2"


def test_run_embed_processes_nodes():
    node_rows = [
        ("uid1", "foo", "Does foo", "def foo(): pass"),
        ("uid2", "bar", None, "def bar(): pass"),
    ]
    provider = _MockProvider(dims=4)

    # We need a conn that returns rows for each table, then empty
    class _MultiTableConn:
        def __init__(self):
            self.writes = []
            self._call_count = {}

        def execute(self, query, *, parameters=None):
            if "SET" in query:
                self.writes.append(parameters)
                return _FakeResult([])
            # Return nodes on first MATCH per table, empty on second
            for table in EMBEDDABLE_TABLES:
                if f"`{table}`" in query and "RETURN" in query:
                    key = table
                    self._call_count.setdefault(key, 0)
                    self._call_count[key] += 1
                    if self._call_count[key] == 1:
                        return _FakeResult(node_rows)
                    return _FakeResult([])
            return _FakeResult([])

    conn = _MultiTableConn()
    result = _run_embed(conn, provider, force=False)

    # 2 nodes per table, 2 tables = 4 total
    assert result["total_embedded"] == 4
    assert result["total_skipped_empty"] == 0
    assert len(result["tables"]) == len(EMBEDDABLE_TABLES)
    # Provider was called once per table batch
    assert len(provider.calls) == len(EMBEDDABLE_TABLES)


def test_run_embed_skips_empty_text_nodes_without_infinite_loop():
    """Empty-text nodes stay WHERE embedding IS NULL.  The loop must
    terminate via seen_uids dedup, not spin forever re-fetching them."""
    node_rows = [
        ("uid1", None, None, None),  # all empty — will never be embedded
    ]
    provider = _MockProvider(dims=4)

    class _RepeatingConn:
        """Simulates a real DB: empty-text nodes keep matching IS NULL
        on every fetch because we never write an embedding for them."""

        def __init__(self):
            self.writes = []
            self.fetch_count = 0

        def execute(self, query, *, parameters=None):
            if "SET" in query:
                self.writes.append(parameters)
                return _FakeResult([])
            for table in EMBEDDABLE_TABLES:
                if f"`{table}`" in query and "RETURN" in query:
                    self.fetch_count += 1
                    # Always return the same row — simulates the bug
                    return _FakeResult(node_rows)
            return _FakeResult([])

    conn = _RepeatingConn()
    result = _run_embed(conn, provider, force=False)

    assert result["total_embedded"] == 0
    assert result["total_skipped_empty"] == len(EMBEDDABLE_TABLES)
    # Critical: the loop must have terminated, not spun indefinitely.
    # With 2 tables and dedup, we expect a small bounded fetch count.
    assert conn.fetch_count <= len(EMBEDDABLE_TABLES) * 2
    assert len(conn.writes) == 0


def test_run_embed_revisits_first_null_batch_after_writes():
    """Non-force embeds must not SKIP past rows after earlier writes shrink the
    WHERE embedding IS NULL result set."""
    provider = _MockProvider(dims=4)

    class _ShrinkingNullConn:
        def __init__(self):
            self.remaining = {
                table: [
                    {"uid": f"{table}-1", "name": "alpha", "docstring": None, "source": "def alpha(): pass"},
                    {"uid": f"{table}-2", "name": "beta", "docstring": None, "source": "def beta(): pass"},
                    {"uid": f"{table}-3", "name": "gamma", "docstring": None, "source": "def gamma(): pass"},
                ]
                for table in EMBEDDABLE_TABLES
            }
            self.writes: list[dict[str, str]] = []

        def execute(self, query, *, parameters=None):
            if "MATCH" in query and "SET" in query:
                self.writes.append(parameters)
                for rows in self.remaining.values():
                    rows[:] = [row for row in rows if row["uid"] != parameters["uid"]]
                return _FakeResult([])

            for table in EMBEDDABLE_TABLES:
                if f"`{table}`" in query and "RETURN" in query:
                    rows = self.remaining[table]
                    skip = 0
                    if "SKIP " in query:
                        skip = int(query.split("SKIP ", 1)[1].split(" ", 1)[0])
                    batch = rows[skip:skip + 2]
                    return _FakeResult([
                        (row["uid"], row["name"], row["docstring"], row["source"])
                        for row in batch
                    ])
            return _FakeResult([])

    conn = _ShrinkingNullConn()
    with patch("codegraphcontext_ext.commands.embed._BATCH_SIZE", 2):
        result = _run_embed(conn, provider, force=False)

    assert result["total_embedded"] == len(EMBEDDABLE_TABLES) * 3
    assert result["total_skipped_empty"] == 0
    assert len(conn.writes) == len(EMBEDDABLE_TABLES) * 3


def test_run_embed_skips_full_empty_first_batch_to_reach_later_nodes():
    """Persistent empty rows must not block later embeddable NULL rows."""
    provider = _MockProvider(dims=4)

    class _EmptyFirstBatchConn:
        def __init__(self):
            self.remaining = {
                table: [
                    {"uid": f"{table}-empty-1", "name": None, "docstring": None, "source": None},
                    {"uid": f"{table}-empty-2", "name": None, "docstring": None, "source": None},
                    {"uid": f"{table}-real", "name": "real", "docstring": None, "source": "def real(): pass"},
                ]
                for table in EMBEDDABLE_TABLES
            }
            self.writes: list[dict[str, str]] = []

        def execute(self, query, *, parameters=None):
            if "MATCH" in query and "SET" in query:
                self.writes.append(parameters)
                for rows in self.remaining.values():
                    rows[:] = [row for row in rows if row["uid"] != parameters["uid"]]
                return _FakeResult([])

            for table in EMBEDDABLE_TABLES:
                if f"`{table}`" in query and "RETURN" in query:
                    rows = self.remaining[table]
                    skip = 0
                    if "SKIP " in query:
                        skip = int(query.split("SKIP ", 1)[1].split(" ", 1)[0])
                    batch = rows[skip:skip + 2]
                    return _FakeResult([
                        (row["uid"], row["name"], row["docstring"], row["source"])
                        for row in batch
                    ])
            return _FakeResult([])

    conn = _EmptyFirstBatchConn()
    with patch("codegraphcontext_ext.commands.embed._BATCH_SIZE", 2):
        result = _run_embed(conn, provider, force=False)

    assert result["total_embedded"] == len(EMBEDDABLE_TABLES)
    assert result["total_skipped_empty"] == len(EMBEDDABLE_TABLES) * 2
    assert len(conn.writes) == len(EMBEDDABLE_TABLES)


def test_run_name_embed_revisits_first_null_batch_after_writes():
    """Name embeddings share the same shrinking-window risk as behavior embeddings."""
    provider = _MockProvider(dims=4)

    class _ShrinkingNameNullConn:
        def __init__(self):
            self.remaining = {
                table: [
                    {"uid": f"{table}-1", "name": "alpha_fn"},
                    {"uid": f"{table}-2", "name": "beta_fn"},
                    {"uid": f"{table}-3", "name": "gamma_fn"},
                ]
                for table in EMBEDDABLE_TABLES
            }
            self.writes: list[dict[str, str]] = []

        def execute(self, query, *, parameters=None):
            if "MATCH" in query and "SET" in query:
                self.writes.append(parameters)
                for rows in self.remaining.values():
                    rows[:] = [row for row in rows if row["uid"] != parameters["uid"]]
                return _FakeResult([])

            for table in EMBEDDABLE_TABLES:
                if f"`{table}`" in query and "RETURN n.uid AS uid, n.name AS name" in query:
                    rows = self.remaining[table]
                    skip = 0
                    if "SKIP " in query:
                        skip = int(query.split("SKIP ", 1)[1].split(" ", 1)[0])
                    batch = rows[skip:skip + 2]
                    return _FakeResult([
                        (row["uid"], row["name"])
                        for row in batch
                    ])
            return _FakeResult([])

    conn = _ShrinkingNameNullConn()
    with patch("codegraphcontext_ext.commands.embed._BATCH_SIZE", 2):
        result = _run_name_embed(conn, provider, force=False)

    assert result["total_embedded"] == len(EMBEDDABLE_TABLES) * 3
    assert result["total_skipped_empty"] == 0
    assert len(conn.writes) == len(EMBEDDABLE_TABLES) * 3


def test_run_name_embed_skips_full_empty_first_batch_to_reach_later_nodes():
    """Persistent empty names must not block later name embeddings."""
    provider = _MockProvider(dims=4)

    class _EmptyFirstNameBatchConn:
        def __init__(self):
            self.remaining = {
                table: [
                    {"uid": f"{table}-empty-1", "name": ""},
                    {"uid": f"{table}-empty-2", "name": None},
                    {"uid": f"{table}-real", "name": "real_fn"},
                ]
                for table in EMBEDDABLE_TABLES
            }
            self.writes: list[dict[str, str]] = []

        def execute(self, query, *, parameters=None):
            if "MATCH" in query and "SET" in query:
                self.writes.append(parameters)
                for rows in self.remaining.values():
                    rows[:] = [row for row in rows if row["uid"] != parameters["uid"]]
                return _FakeResult([])

            for table in EMBEDDABLE_TABLES:
                if f"`{table}`" in query and "RETURN n.uid AS uid, n.name AS name" in query:
                    rows = self.remaining[table]
                    skip = 0
                    if "SKIP " in query:
                        skip = int(query.split("SKIP ", 1)[1].split(" ", 1)[0])
                    batch = rows[skip:skip + 2]
                    return _FakeResult([
                        (row["uid"], row["name"])
                        for row in batch
                    ])
            return _FakeResult([])

    conn = _EmptyFirstNameBatchConn()
    with patch("codegraphcontext_ext.commands.embed._BATCH_SIZE", 2):
        result = _run_name_embed(conn, provider, force=False)

    assert result["total_embedded"] == len(EMBEDDABLE_TABLES)
    assert result["total_skipped_empty"] == len(EMBEDDABLE_TABLES) * 2
    assert len(conn.writes) == len(EMBEDDABLE_TABLES)


def test_embed_write_path_emits_json(monkeypatch):
    """End-to-end: embed command write path emits valid JSON with embed_complete."""
    monkeypatch.setenv("DEFAULT_DATABASE", "kuzudb")
    monkeypatch.setattr(runtime, "is_kuzudb_available", lambda: True)

    fake_conn = _FakeConn()
    # Make the conn return no nodes (empty graph)
    fake_conn.execute = lambda q, **kw: _FakeResult([])

    mock_provider = _MockProvider(dims=768)

    with patch(
        "codegraphcontext_ext.commands.embed.get_kuzu_connection",
        return_value=fake_conn,
    ), patch(
        "codegraphcontext_ext.commands.embed.create_provider",
        return_value=mock_provider,
    ), patch(
        "codegraphcontext_ext.commands.embed.ensure_embedding_columns",
        return_value=[{"table": "Function", "action": "exists", "detail": "ok"}],
    ), patch(
        "codegraphcontext_ext.commands.embed.ensure_hnsw_indexes",
        return_value=[{"table": "Function", "action": "exists", "detail": "ok"}],
    ):
        app = _embed_app()
        result = runner.invoke(app, ["embed"])

    assert result.exit_code == 0
    payload = _extract_json(result.output)
    assert payload["ok"] is True
    assert payload["kind"] == "embed_complete"
    assert payload["total_embedded"] == 0


def test_embed_force_flag_passed_through(monkeypatch):
    """--force flag is reflected in the output payload."""
    monkeypatch.setenv("DEFAULT_DATABASE", "kuzudb")
    monkeypatch.setattr(runtime, "is_kuzudb_available", lambda: True)

    fake_conn = _FakeConn()
    fake_conn.execute = lambda q, **kw: _FakeResult([])

    with patch(
        "codegraphcontext_ext.commands.embed.get_kuzu_connection",
        return_value=fake_conn,
    ), patch(
        "codegraphcontext_ext.commands.embed.create_provider",
        return_value=_MockProvider(dims=768),
    ), patch(
        "codegraphcontext_ext.commands.embed.ensure_embedding_columns",
        return_value=[],
    ), patch(
        "codegraphcontext_ext.commands.embed.ensure_hnsw_indexes",
        return_value=[],
    ):
        app = _embed_app()
        result = runner.invoke(app, ["embed", "--force"])

    assert result.exit_code == 0
    payload = _extract_json(result.output)
    assert payload["force"] is True
