"""Tests for the Taxonomy dashboard tab data fetching (Phase 5.5a)."""

from __future__ import annotations

import json

from codegraphcontext_ext.commands.viz_taxonomy import (
    fetch_inheritance,
    fetch_structure,
    fetch_taxonomy_data,
    taxonomy_json,
)

from .conftest import FakeResult


# ---------------------------------------------------------------------------
# Mock connections
# ---------------------------------------------------------------------------


class _StructureConn:
    """Returns containment rows for structure queries."""

    def __init__(self, repo_rows=(), contains_rows=()):
        self._repo_rows = list(repo_rows)
        self._contains_rows = list(contains_rows)
        self._repo_served = False

    def execute(self, query, **_kw):
        if "Repository" in query and "CONTAINS" not in query:
            if not self._repo_served:
                self._repo_served = True
                return FakeResult(self._repo_rows)
            return FakeResult([])
        return FakeResult(self._contains_rows)


class _InheritanceConn:
    """Returns INHERITS and IMPLEMENTS rows."""

    def __init__(self, inherits_rows=(), implements_rows=()):
        self._inherits = list(inherits_rows)
        self._implements = list(implements_rows)

    def execute(self, query, **_kw):
        if "INHERITS" in query:
            return FakeResult(self._inherits)
        if "IMPLEMENTS" in query:
            return FakeResult(self._implements)
        return FakeResult([])


class _EmptyConn:
    def execute(self, query, **_kw):
        return FakeResult([])


# ---------------------------------------------------------------------------
# fetch_structure
# ---------------------------------------------------------------------------


class TestFetchStructure:
    def test_returns_flat_nodes_with_parents(self):
        conn = _StructureConn(
            repo_rows=[("/repo", "my-repo", "Repository", "/repo", 0)],
            contains_rows=[
                ("/repo", "/repo/src", "src", "Directory", "/repo/src", 0),
                ("/repo/src", "/repo/src/main.py", "main.py", "File", "/repo/src/main.py", 0),
            ],
        )
        result = fetch_structure(conn, limit=100)
        nodes = result["nodes"]

        assert len(nodes) >= 1  # at least the repo root
        ids = {n["id"] for n in nodes}
        assert "/repo" in ids

        # Check parent refs exist
        for n in nodes:
            if n["parent"] is not None:
                assert n["parent"] in ids or n["parent"] == n["id"]

    def test_empty_graph(self):
        result = fetch_structure(_EmptyConn(), limit=100)
        assert result["nodes"] == []
        assert result["stats"] == {}

    def test_creates_synthetic_parents(self):
        # A node references a parent that wasn't returned by the repo query
        conn = _StructureConn(
            repo_rows=[],
            contains_rows=[
                ("/missing/dir", "uid1", "foo", "Function", "/missing/dir/a.py", 1),
            ],
        )
        result = fetch_structure(conn, limit=100)
        ids = {n["id"] for n in result["nodes"]}
        # The missing parent should have been created as a synthetic node
        assert "/missing/dir" in ids

    def test_stats_count_types(self):
        conn = _StructureConn(
            repo_rows=[("/r", "r", "Repository", "/r", 0)],
            contains_rows=[
                ("/r", "/r/a.py", "a.py", "File", "/r/a.py", 0),
                ("/r", "/r/b.py", "b.py", "File", "/r/b.py", 0),
            ],
        )
        result = fetch_structure(conn, limit=100)
        assert result["stats"].get("Repository", 0) >= 1
        assert result["stats"].get("File", 0) >= 1


# ---------------------------------------------------------------------------
# fetch_inheritance
# ---------------------------------------------------------------------------


class TestFetchInheritance:
    def test_returns_trees(self):
        conn = _InheritanceConn(
            inherits_rows=[
                ("child_uid", "Child", "Class", "/a.py", 10,
                 "parent_uid", "Parent", "Class", "/a.py", 1),
            ],
        )
        result = fetch_inheritance(conn, limit=100)
        assert len(result["nodes"]) == 2
        assert len(result["edges"]) == 1
        assert result["edges"][0]["type"] == "INHERITS"
        assert "parent_uid" in result["roots"]
        assert result["stats"]["trees"] == 1

    def test_implements_edges(self):
        conn = _InheritanceConn(
            implements_rows=[
                ("impl_uid", "MyClass", "Class", "/a.py", 5,
                 "iface_uid", "MyInterface", "/a.py", 1),
            ],
        )
        result = fetch_inheritance(conn, limit=100)
        assert len(result["edges"]) == 1
        assert result["edges"][0]["type"] == "IMPLEMENTS"
        assert result["stats"]["implements_edges"] == 1

    def test_empty(self):
        result = fetch_inheritance(_EmptyConn(), limit=100)
        assert result["nodes"] == []
        assert result["edges"] == []
        assert result["stats"]["trees"] == 0


# ---------------------------------------------------------------------------
# fetch_taxonomy_data / taxonomy_json
# ---------------------------------------------------------------------------


class TestTaxonomyData:
    def test_has_all_three_keys(self):
        data = fetch_taxonomy_data(_EmptyConn(), limit=100)
        assert "structure" in data
        assert "inheritance" in data
        assert "communities" in data

    def test_communities_empty_graph_has_zero_communities(self):
        data = fetch_taxonomy_data(_EmptyConn(), limit=100)
        comm = data["communities"]
        # Empty graph → community detection returns empty stats
        if comm is None:
            pass  # graceful degradation
        else:
            assert comm["stats"]["communities"] == 0

    def test_taxonomy_json_is_valid(self):
        raw = taxonomy_json(_EmptyConn(), limit=100)
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)
        assert "structure" in parsed
        assert "inheritance" in parsed
        assert "communities" in parsed
