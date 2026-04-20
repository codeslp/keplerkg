"""Tests for HDBSCAN density-based semantic clustering overlay."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from codegraphcontext_ext.topology.hdbscan_overlay import (
    _fetch_all_embeddings,
    cluster_embeddings,
    get_cluster_uids,
)


# ---------------------------------------------------------------------------
# Fake DB helpers
# ---------------------------------------------------------------------------

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
    """Mock connection returning canned embedding rows per table."""

    def __init__(self, rows_by_table: dict[str, list] | None = None):
        self._rows_by_table = rows_by_table or {}

    def execute(self, query, **kwargs):
        for table, rows in self._rows_by_table.items():
            if f"`{table}`" in query:
                return _FakeResult(rows)
        return _FakeResult([])


# ---------------------------------------------------------------------------
# _fetch_all_embeddings
# ---------------------------------------------------------------------------

def test_fetch_embeddings_across_tables():
    conn = _FakeConn(rows_by_table={
        "Function": [
            ("f1", "fn_a", "a.py", [0.1, 0.2]),
            ("f2", "fn_b", "b.py", [0.3, 0.4]),
        ],
        "Class": [
            ("c1", "cls_a", "c.py", [0.5, 0.6]),
        ],
    })
    nodes = _fetch_all_embeddings(conn, max_nodes=100)
    assert len(nodes) == 3
    assert {n["table"] for n in nodes} == {"Function", "Class"}
    assert all("vec" in n for n in nodes)


def test_fetch_embeddings_skips_null():
    conn = _FakeConn(rows_by_table={
        "Function": [
            ("f1", "fn_a", "a.py", [0.1, 0.2]),
            (None, "fn_b", "b.py", [0.3, 0.4]),  # null uid
            ("f3", "fn_c", "c.py", None),          # null embedding
        ],
        "Class": [],
    })
    nodes = _fetch_all_embeddings(conn, max_nodes=100)
    assert len(nodes) == 1
    assert nodes[0]["uid"] == "f1"


# ---------------------------------------------------------------------------
# cluster_embeddings
# ---------------------------------------------------------------------------

def _make_clusterable_conn(n_per_group: int = 10, dims: int = 8) -> _FakeConn:
    """Build a conn with two tight clusters in embedding space."""
    import numpy as np
    rng = np.random.RandomState(42)

    rows = []
    # Cluster A: centered at [1, 0, 0, ...]
    for i in range(n_per_group):
        vec = rng.normal(loc=1.0, scale=0.05, size=dims).tolist()
        rows.append((f"a{i}", f"fn_a{i}", "group_a.py", vec))

    # Cluster B: centered at [-1, 0, 0, ...]
    for i in range(n_per_group):
        vec = rng.normal(loc=-1.0, scale=0.05, size=dims).tolist()
        rows.append((f"b{i}", f"fn_b{i}", "group_b.py", vec))

    return _FakeConn(rows_by_table={"Function": rows, "Class": []})


def test_cluster_embeddings_finds_clusters():
    conn = _make_clusterable_conn(n_per_group=10)
    result = cluster_embeddings(conn, min_cluster_size=5, min_samples=3)

    assert result["ok"] is True
    assert result["total_nodes"] == 20
    assert result["cluster_count"] >= 2
    assert result["noise_count"] >= 0

    # Verify cluster members have required fields
    for cluster in result["clusters"]:
        assert "id" in cluster
        assert "size" in cluster
        assert "members" in cluster
        assert cluster["size"] == len(cluster["members"])
        for m in cluster["members"]:
            assert "uid" in m
            assert "name" in m
            assert "path" in m
            assert "table" in m


def test_cluster_embeddings_insufficient_nodes():
    conn = _FakeConn(rows_by_table={
        "Function": [("f1", "fn_a", "a.py", [0.1, 0.2])],
        "Class": [],
    })
    result = cluster_embeddings(conn, min_cluster_size=5)
    assert result["ok"] is False
    assert result["reason"] == "insufficient_embeddings"


def test_cluster_embeddings_empty_db():
    conn = _FakeConn(rows_by_table={"Function": [], "Class": []})
    result = cluster_embeddings(conn, min_cluster_size=5)
    assert result["ok"] is False
    assert result["total_nodes"] == 0


def test_cluster_embeddings_all_noise():
    """When points are very scattered, HDBSCAN may label everything as noise."""
    import numpy as np
    rng = np.random.RandomState(99)
    rows = [
        (f"f{i}", f"fn_{i}", f"file_{i}.py", rng.uniform(-100, 100, size=8).tolist())
        for i in range(10)
    ]
    conn = _FakeConn(rows_by_table={"Function": rows, "Class": []})
    result = cluster_embeddings(conn, min_cluster_size=5, min_samples=5)

    assert result["ok"] is True
    # All noise → 0 clusters
    assert result["noise_count"] + sum(c["size"] for c in result["clusters"]) == 10


# ---------------------------------------------------------------------------
# get_cluster_uids
# ---------------------------------------------------------------------------

def test_get_cluster_uids_returns_correct_set():
    conn = _make_clusterable_conn(n_per_group=10)
    result = cluster_embeddings(conn, min_cluster_size=5, min_samples=3)
    if not result["ok"] or result["cluster_count"] == 0:
        pytest.skip("HDBSCAN did not find clusters with this random seed")

    first_cluster_id = result["clusters"][0]["id"]
    expected_uids = {m["uid"] for m in result["clusters"][0]["members"]}

    uids = get_cluster_uids(conn, first_cluster_id, min_cluster_size=5, min_samples=3)
    assert uids == expected_uids


def test_get_cluster_uids_nonexistent_cluster():
    conn = _make_clusterable_conn(n_per_group=10)
    uids = get_cluster_uids(conn, 9999, min_cluster_size=5, min_samples=3)
    assert uids == set()


def test_get_cluster_uids_empty_db():
    conn = _FakeConn(rows_by_table={"Function": [], "Class": []})
    uids = get_cluster_uids(conn, 0, min_cluster_size=5)
    assert uids == set()
