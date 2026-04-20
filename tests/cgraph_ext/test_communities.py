"""Tests for Phase 5.5b community detection (topology/communities.py)."""

from __future__ import annotations

import networkx as nx
import pytest

from codegraphcontext_ext.topology.communities import (
    _compute_semantic_edges,
    build_combined_graph,
    cross_community_edges,
    detect_communities,
    fetch_community_data,
    score_cross_community_surprise,
)

from .conftest import FakeResult


# ---------------------------------------------------------------------------
# _compute_semantic_edges
# ---------------------------------------------------------------------------


class TestComputeSemanticEdges:
    def test_identical_vectors_produce_edge(self):
        nodes = [
            {"uid": "a", "name": "a", "path": "a.py", "vec": [1.0, 0.0, 0.0]},
            {"uid": "b", "name": "b", "path": "b.py", "vec": [1.0, 0.0, 0.0]},
        ]
        edges = _compute_semantic_edges(nodes, threshold=0.85)
        assert len(edges) == 1
        assert edges[0][2] == pytest.approx(1.0)

    def test_orthogonal_vectors_no_edge(self):
        nodes = [
            {"uid": "a", "name": "a", "path": "a.py", "vec": [1.0, 0.0]},
            {"uid": "b", "name": "b", "path": "b.py", "vec": [0.0, 1.0]},
        ]
        edges = _compute_semantic_edges(nodes, threshold=0.85)
        assert len(edges) == 0

    def test_threshold_configurable(self):
        nodes = [
            {"uid": "a", "name": "a", "path": "a.py", "vec": [1.0, 0.5]},
            {"uid": "b", "name": "b", "path": "b.py", "vec": [1.0, 0.0]},
        ]
        # These vectors have cosine ~0.89
        low_thresh = _compute_semantic_edges(nodes, threshold=0.5)
        high_thresh = _compute_semantic_edges(nodes, threshold=0.95)
        assert len(low_thresh) == 1
        assert len(high_thresh) == 0

    def test_empty_nodes(self):
        assert _compute_semantic_edges([], threshold=0.85) == []


# ---------------------------------------------------------------------------
# detect_communities
# ---------------------------------------------------------------------------


class TestDetectCommunities:
    def test_two_clusters(self):
        G = nx.Graph()
        # Cluster 1: tightly connected
        G.add_edges_from([("a", "b"), ("b", "c"), ("a", "c")])
        # Cluster 2: tightly connected
        G.add_edges_from([("d", "e"), ("e", "f"), ("d", "f")])
        # Weak bridge
        G.add_edge("c", "d")

        communities = detect_communities(G)
        assert len(communities) >= 2

        # Each original cluster should be in the same community
        node_to_comm = {}
        for i, comm in enumerate(communities):
            for n in comm:
                node_to_comm[n] = i
        assert node_to_comm["a"] == node_to_comm["b"] == node_to_comm["c"]
        assert node_to_comm["d"] == node_to_comm["e"] == node_to_comm["f"]

    def test_empty_graph(self):
        G = nx.Graph()
        assert detect_communities(G) == []

    def test_single_node(self):
        G = nx.Graph()
        G.add_node("a")
        communities = detect_communities(G)
        assert len(communities) == 1
        assert "a" in communities[0]


# ---------------------------------------------------------------------------
# cross_community_edges
# ---------------------------------------------------------------------------


class TestCrossCommunityEdges:
    def test_finds_crossing_edges(self):
        G = nx.Graph()
        G.add_edge("a", "b", type="CALLS")
        G.add_edge("b", "c", type="CALLS")  # cross-community

        communities = [{"a", "b"}, {"c"}]
        cross = cross_community_edges(G, communities)
        assert len(cross) == 1
        assert cross[0]["type"] == "CALLS"

    def test_no_crossing_edges(self):
        G = nx.Graph()
        G.add_edge("a", "b", type="CALLS")
        communities = [{"a", "b"}]
        cross = cross_community_edges(G, communities)
        assert len(cross) == 0


# ---------------------------------------------------------------------------
# Mock connection for integration tests
# ---------------------------------------------------------------------------


class _CommunityConn:
    """Mock connection returning structural edges and embeddings."""

    def __init__(self, calls_rows=(), imports_rows=(), emb_rows=()):
        self._calls = list(calls_rows)
        self._imports = list(imports_rows)
        self._emb = list(emb_rows)

    def execute(self, query, **_kw):
        if "CALLS" in query and "Function" in query:
            return FakeResult(self._calls)
        if "IMPORTS" in query:
            return FakeResult(self._imports)
        if "embedding" in query.lower() or "EMBEDDING" in query:
            return FakeResult(self._emb)
        return FakeResult([])


class TestBuildCombinedGraph:
    def test_structural_only(self):
        conn = _CommunityConn(
            calls_rows=[("uid1", "uid2"), ("uid2", "uid3")],
        )
        G, meta = build_combined_graph(conn, include_semantic=False)
        assert len(G.edges()) == 2
        assert all(d["provenance"] == "extracted" for _, _, d in G.edges(data=True))

    def test_with_semantic_edges(self):
        conn = _CommunityConn(
            calls_rows=[("uid1", "uid2")],
            emb_rows=[
                ("uid1", "foo", "a.py", [1.0, 0.0, 0.0]),
                ("uid2", "bar", "b.py", [1.0, 0.0, 0.0]),  # identical = cosine 1.0
            ],
        )
        G, meta = build_combined_graph(conn, semantic_threshold=0.85)
        # Should have structural edge + semantic edge
        types = {d["type"] for _, _, d in G.edges(data=True)}
        assert "CALLS" in types
        assert "SEMANTICALLY_SIMILAR" in types

    def test_empty(self):
        conn = _CommunityConn()
        G, meta = build_combined_graph(conn, include_semantic=False)
        assert len(G.nodes()) == 0


class TestFetchCommunityData:
    def test_returns_correct_shape(self):
        conn = _CommunityConn(
            calls_rows=[("a", "b"), ("b", "c"), ("c", "a")],
        )
        data = fetch_community_data(conn, max_semantic_nodes=0)
        assert "communities" in data
        assert "edges" in data
        assert "cross_edges" in data
        assert "stats" in data
        assert data["stats"]["total_nodes"] == 3
        assert data["stats"]["communities"] >= 1

    def test_empty_graph(self):
        conn = _CommunityConn()
        data = fetch_community_data(conn, max_semantic_nodes=0)
        assert data["stats"]["total_nodes"] == 0
        assert data["stats"]["communities"] == 0

    def test_provenance_tracking(self):
        conn = _CommunityConn(
            calls_rows=[("a", "b")],
            emb_rows=[
                ("a", "foo", "a.py", [1.0, 0.0]),
                ("b", "bar", "b.py", [1.0, 0.0]),
            ],
        )
        data = fetch_community_data(conn, semantic_threshold=0.5)
        provenances = {e["provenance"] for e in data["edges"]}
        assert "extracted" in provenances
        assert "inferred" in provenances


# ---------------------------------------------------------------------------
# score_cross_community_surprise
# ---------------------------------------------------------------------------


class TestSurpriseScoring:
    def test_empty_edges(self):
        result = score_cross_community_surprise([], [])
        assert result == []

    def test_single_edge_between_large_communities(self):
        """One edge between two 10-node communities → high surprise."""
        comms = [set(f"a{i}" for i in range(10)), set(f"b{i}" for i in range(10))]
        edges = [{
            "source": "a0", "target": "b0", "type": "CALLS",
            "provenance": "extracted", "source_community": 0, "target_community": 1,
        }]
        result = score_cross_community_surprise(comms, edges)
        assert len(result) == 1
        assert "surprise" in result[0]
        # 1 edge out of 100 possible → -log2(1/100) ≈ 6.64
        assert result[0]["surprise"] > 6.0

    def test_many_edges_between_communities_low_surprise(self):
        """Many edges between small communities → low surprise."""
        comms = [{"a0", "a1"}, {"b0", "b1"}]
        edges = [
            {"source": "a0", "target": "b0", "type": "CALLS",
             "provenance": "extracted", "source_community": 0, "target_community": 1},
            {"source": "a1", "target": "b1", "type": "CALLS",
             "provenance": "extracted", "source_community": 0, "target_community": 1},
            {"source": "a0", "target": "b1", "type": "CALLS",
             "provenance": "extracted", "source_community": 0, "target_community": 1},
        ]
        result = score_cross_community_surprise(comms, edges)
        # 3 edges out of 4 possible → -log2(3/4) ≈ 0.42
        assert all(e["surprise"] < 1.0 for e in result)

    def test_sorted_descending_by_surprise(self):
        """Result is sorted by surprise descending."""
        comms = [set(f"a{i}" for i in range(10)), {"b0"}, {"c0", "c1"}]
        edges = [
            {"source": "a0", "target": "b0", "type": "CALLS",
             "provenance": "extracted", "source_community": 0, "target_community": 1},
            {"source": "c0", "target": "c1", "type": "CALLS",
             "provenance": "extracted", "source_community": 2, "target_community": 2},
            {"source": "a0", "target": "c0", "type": "CALLS",
             "provenance": "extracted", "source_community": 0, "target_community": 2},
        ]
        # Only cross-community edges get scored (c0→c1 is same community, won't be in the list)
        cross_only = [e for e in edges if e["source_community"] != e["target_community"]]
        result = score_cross_community_surprise(comms, cross_only)
        for i in range(len(result) - 1):
            assert result[i]["surprise"] >= result[i + 1]["surprise"]

    def test_surprise_capped_at_10(self):
        """Very sparse connections cap at 10."""
        comms = [set(f"a{i}" for i in range(100)), set(f"b{i}" for i in range(100))]
        edges = [{
            "source": "a0", "target": "b0", "type": "CALLS",
            "provenance": "extracted", "source_community": 0, "target_community": 1,
        }]
        result = score_cross_community_surprise(comms, edges)
        assert result[0]["surprise"] <= 10.0
