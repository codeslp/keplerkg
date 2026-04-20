"""HDBSCAN density-based clustering over KùzuDB code embeddings.

Fetches behavior embeddings from Function/Class nodes, runs sklearn
HDBSCAN, and returns cluster assignments with noise detection.  This
complements the Louvain graph-based communities in ``communities.py``
by finding density-based groups in embedding space.

Phase 7 — spec 001-progress §7+.
"""

from __future__ import annotations

from typing import Any

from ..embeddings.schema import EMBEDDABLE_TABLES, EMBEDDING_COLUMN


def _fetch_all_embeddings(
    conn: Any,
    *,
    max_nodes: int = 3000,
) -> list[dict[str, Any]]:
    """Fetch nodes with embeddings across all embeddable tables."""
    nodes: list[dict[str, Any]] = []
    for table in EMBEDDABLE_TABLES:
        query = (
            f"MATCH (n:`{table}`) "
            f"WHERE n.`{EMBEDDING_COLUMN}` IS NOT NULL "
            f"  AND NOT n.is_dependency "
            f"RETURN n.uid, n.name, n.path, n.`{EMBEDDING_COLUMN}` "
            f"LIMIT {max_nodes}"
        )
        try:
            result = conn.execute(query)
            while result.has_next():
                row = result.get_next()
                if row[0] and row[3]:
                    nodes.append({
                        "uid": str(row[0]),
                        "name": row[1],
                        "path": row[2],
                        "table": table,
                        "vec": list(row[3]),
                    })
        except Exception:
            pass
    return nodes


def cluster_embeddings(
    conn: Any,
    *,
    min_cluster_size: int = 5,
    min_samples: int = 3,
    max_nodes: int = 3000,
) -> dict[str, Any]:
    """Run HDBSCAN on graph embeddings and return cluster assignments.

    Returns a dict with:
      - ok: bool
      - total_nodes: int
      - noise_count: int
      - cluster_count: int
      - clusters: list of {id, size, members: [{uid, name, path, table}]}
    """
    try:
        import numpy as np
        from sklearn.cluster import HDBSCAN
    except ImportError:
        return {
            "ok": False,
            "reason": "missing_dependency",
            "error": "scikit-learn is required for HDBSCAN clustering.",
            "total_nodes": 0,
            "noise_count": 0,
            "cluster_count": 0,
            "clusters": [],
        }

    nodes = _fetch_all_embeddings(conn, max_nodes=max_nodes)
    if len(nodes) < min_cluster_size:
        return {
            "ok": False,
            "reason": "insufficient_embeddings",
            "error": f"Need at least {min_cluster_size} embedded nodes, found {len(nodes)}.",
            "total_nodes": len(nodes),
            "noise_count": 0,
            "cluster_count": 0,
            "clusters": [],
        }

    vectors = np.array([n["vec"] for n in nodes], dtype=np.float32)

    clusterer = HDBSCAN(
        min_cluster_size=min_cluster_size,
        min_samples=min_samples,
        metric="euclidean",
    )
    labels = clusterer.fit_predict(vectors)

    # Group by cluster label
    cluster_map: dict[int, list[dict[str, Any]]] = {}
    noise_count = 0
    for i, label in enumerate(labels):
        if label == -1:
            noise_count += 1
            continue
        label_int = int(label)
        if label_int not in cluster_map:
            cluster_map[label_int] = []
        cluster_map[label_int].append({
            "uid": nodes[i]["uid"],
            "name": nodes[i]["name"],
            "path": nodes[i]["path"],
            "table": nodes[i]["table"],
        })

    clusters = [
        {"id": cid, "size": len(members), "members": members}
        for cid, members in sorted(cluster_map.items())
    ]

    return {
        "ok": True,
        "total_nodes": len(nodes),
        "noise_count": noise_count,
        "cluster_count": len(clusters),
        "clusters": clusters,
    }


def get_cluster_uids(
    conn: Any,
    cluster_id: int,
    *,
    min_cluster_size: int = 5,
    min_samples: int = 3,
    max_nodes: int = 3000,
) -> set[str]:
    """Return the set of UIDs belonging to a specific HDBSCAN cluster.

    Used by the context command to scope ANN results to a cluster.
    Returns empty set if clustering fails or cluster_id not found.
    """
    result = cluster_embeddings(
        conn,
        min_cluster_size=min_cluster_size,
        min_samples=min_samples,
        max_nodes=max_nodes,
    )
    if not result["ok"]:
        return set()
    for cluster in result["clusters"]:
        if cluster["id"] == cluster_id:
            return {m["uid"] for m in cluster["members"]}
    return set()
