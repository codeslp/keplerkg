"""Community detection on the combined structural + semantic code graph.

Builds a NetworkX graph from KuzuDB CALLS/IMPORTS edges, optionally adds
SEMANTICALLY_SIMILAR edges from embedding cosine similarity, then runs
Louvain community detection.  Returns community assignments and cross-
community edge statistics.

Phase 5.5b.  Graphify/EdgeQuake research validated this approach.
"""

from __future__ import annotations

import math
import sys
from collections import defaultdict
from typing import Any

import networkx as nx

from ..embeddings.schema import EMBEDDING_COLUMN, EMBEDDABLE_TABLES
from ..hybrid.ann import cosine_similarity


# ── Graph construction ─────────────────────────────────────────────────

def _fetch_structural_edges(conn: Any) -> list[tuple[str, str, str]]:
    """Fetch CALLS and IMPORTS edges as (source_id, target_id, edge_type) triples."""
    edges: list[tuple[str, str, str]] = []

    # CALLS: Function→Function
    try:
        result = conn.execute(
            "MATCH (a:Function)-[:CALLS]->(b:Function) "
            "RETURN a.uid, b.uid LIMIT 5000"
        )
        while result.has_next():
            row = result.get_next()
            if row[0] and row[1]:
                edges.append((str(row[0]), str(row[1]), "CALLS"))
    except Exception:
        pass

    # IMPORTS: File→Module (map to file-level for community grouping)
    try:
        result = conn.execute(
            "MATCH (f:File)-[:IMPORTS]->(m:Module) "
            "RETURN f.path, m.name LIMIT 5000"
        )
        while result.has_next():
            row = result.get_next()
            if row[0] and row[1]:
                edges.append((str(row[0]), str(row[1]), "IMPORTS"))
    except Exception:
        pass

    return edges


def _fetch_embeddings_for_similarity(
    conn: Any,
    *,
    max_nodes: int = 2000,
) -> list[dict[str, Any]]:
    """Fetch Function nodes with behavior embeddings for semantic edge computation."""
    nodes: list[dict[str, Any]] = []
    query = (
        f"MATCH (n:Function) "
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
                    "vec": list(row[3]),
                })
    except Exception:
        pass
    return nodes


def _compute_semantic_edges(
    nodes: list[dict[str, Any]],
    *,
    threshold: float = 0.85,
) -> list[tuple[str, str, float]]:
    """Compute SEMANTICALLY_SIMILAR edges from embedding cosine similarity.

    Returns (uid_a, uid_b, similarity) triples where similarity > threshold.
    Only considers pairs — O(n^2) but capped by max_nodes in the caller.
    """
    edges: list[tuple[str, str, float]] = []
    for i, a in enumerate(nodes):
        for b in nodes[i + 1:]:
            sim = cosine_similarity(a["vec"], b["vec"])
            if sim > threshold:
                edges.append((a["uid"], b["uid"], round(sim, 4)))
    return edges


def build_combined_graph(
    conn: Any,
    *,
    semantic_threshold: float = 0.85,
    max_semantic_nodes: int = 2000,
    include_semantic: bool = True,
) -> tuple[nx.Graph, dict[str, dict[str, Any]]]:
    """Build a NetworkX graph from structural + semantic edges.

    Returns (graph, node_metadata) where node_metadata maps uid to
    {name, path, type} for labeling.
    """
    G = nx.MultiGraph()
    node_meta: dict[str, dict[str, Any]] = {}

    # Add structural edges
    structural = _fetch_structural_edges(conn)
    for src, tgt, etype in structural:
        G.add_edge(src, tgt, type=etype, provenance="extracted", confidence=1.0)

    # Add semantic edges from embeddings
    semantic_count = 0
    if include_semantic:
        emb_nodes = _fetch_embeddings_for_similarity(conn, max_nodes=max_semantic_nodes)
        for n in emb_nodes:
            node_meta[n["uid"]] = {"name": n["name"], "path": n["path"], "type": "Function"}
        sem_edges = _compute_semantic_edges(emb_nodes, threshold=semantic_threshold)
        for uid_a, uid_b, sim in sem_edges:
            G.add_edge(uid_a, uid_b, type="SEMANTICALLY_SIMILAR",
                       provenance="inferred", confidence=sim)
            semantic_count += 1

    # Collect node metadata for nodes added via structural edges but
    # not in the embedding fetch (e.g., Module nodes from IMPORTS)
    for node_id in G.nodes():
        if node_id not in node_meta:
            node_meta[node_id] = {"name": node_id.split("/")[-1], "path": str(node_id), "type": "unknown"}

    return G, node_meta


# ── Community detection ────────────────────────────────────────────────

def detect_communities(
    G: nx.Graph,
    *,
    resolution: float = 1.0,
    max_community_fraction: float = 0.25,
) -> list[set[str]]:
    """Run Louvain community detection on the graph.

    Returns a list of sets, each set containing the node IDs in one community.
    Oversized communities (> max_community_fraction of total nodes) are
    recursively split with a higher resolution.
    """
    if len(G.nodes()) == 0:
        return []

    communities = list(nx.community.louvain_communities(G, resolution=resolution, seed=42))

    # Recursive split for oversized communities (Graphify pattern)
    total = len(G.nodes())
    max_size = int(total * max_community_fraction)
    if max_size < 3:
        max_size = 3

    refined: list[set[str]] = []
    for comm in communities:
        if len(comm) > max_size and len(comm) > 3:
            subgraph = G.subgraph(comm)
            sub_comms = detect_communities(
                subgraph.copy(),
                resolution=resolution * 1.5,
                max_community_fraction=max_community_fraction,
            )
            refined.extend(sub_comms)
        else:
            refined.append(comm)

    return refined


# ── Cross-community analysis ──────────────────────────────────────────

def cross_community_edges(
    G: nx.Graph,
    communities: list[set[str]],
) -> list[dict[str, Any]]:
    """Find edges that cross community boundaries — potential coupling smells.

    Returns a list of dicts: {source, target, type, provenance, source_community, target_community}.
    """
    # Build node→community index
    node_to_comm: dict[str, int] = {}
    for i, comm in enumerate(communities):
        for node_id in comm:
            node_to_comm[node_id] = i

    crossing: list[dict[str, Any]] = []
    for u, v, data in G.edges(data=True):
        cu = node_to_comm.get(u, -1)
        cv = node_to_comm.get(v, -1)
        if cu != cv and cu >= 0 and cv >= 0:
            crossing.append({
                "source": u,
                "target": v,
                "type": data.get("type", "unknown"),
                "provenance": data.get("provenance", "extracted"),
                "source_community": cu,
                "target_community": cv,
            })
    return crossing


def score_cross_community_surprise(
    communities: list[set[str]],
    cross_edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Score cross-community edges by architectural surprise.

    Surprise = log2(expected_density / actual_density) for the community
    pair, clamped to [0, 10].  High surprise means the edge connects
    communities that share very few links relative to their size — a
    potential architecture smell.

    Adds ``surprise`` (float) to each cross-edge dict in-place and returns
    the annotated list sorted by descending surprise.
    """
    if not communities or not cross_edges:
        return cross_edges

    # Count edges per community pair
    pair_counts: dict[tuple[int, int], int] = {}
    for edge in cross_edges:
        pair = (
            min(edge["source_community"], edge["target_community"]),
            max(edge["source_community"], edge["target_community"]),
        )
        pair_counts[pair] = pair_counts.get(pair, 0) + 1

    # Community sizes
    comm_sizes = {i: len(c) for i, c in enumerate(communities)}

    # Score each edge
    for edge in cross_edges:
        ca = edge["source_community"]
        cb = edge["target_community"]
        pair = (min(ca, cb), max(ca, cb))
        size_a = comm_sizes.get(ca, 1)
        size_b = comm_sizes.get(cb, 1)
        possible = size_a * size_b
        actual = pair_counts.get(pair, 1)

        # Surprise: how sparse is this cross-boundary connection?
        # density = actual / possible; surprise = -log2(density) capped at 10
        density = actual / max(possible, 1)
        if density <= 0:
            surprise = 10.0
        else:
            surprise = min(10.0, max(0.0, -math.log2(density)))
        edge["surprise"] = round(surprise, 2)

    cross_edges.sort(key=lambda e: e.get("surprise", 0), reverse=True)
    return cross_edges


# ── Public API for dashboard ───────────────────────────────────────────

def fetch_community_data(
    conn: Any,
    *,
    semantic_threshold: float = 0.85,
    max_semantic_nodes: int = 2000,
) -> dict[str, Any]:
    """Fetch community detection results.

    Returns:
    {
        "communities": [
            {"id": 0, "size": N, "members": [{"uid": ..., "name": ..., "path": ...}]},
            ...
        ],
        "edges": [{"source": ..., "target": ..., "type": ..., "community": ...}],
        "cross_edges": [...],
        "stats": {
            "total_nodes": N, "total_edges": N,
            "communities": N, "structural_edges": N, "semantic_edges": N,
            "cross_community_edges": N,
        },
    }
    """
    print("  building combined graph...", file=sys.stderr)
    G, node_meta = build_combined_graph(
        conn,
        semantic_threshold=semantic_threshold,
        max_semantic_nodes=max_semantic_nodes,
    )

    if len(G.nodes()) == 0:
        return {
            "communities": [],
            "edges": [],
            "cross_edges": [],
            "stats": {
                "total_nodes": 0, "total_edges": 0,
                "communities": 0, "structural_edges": 0,
                "semantic_edges": 0, "cross_community_edges": 0,
            },
        }

    print(f"  graph: {len(G.nodes())} nodes, {len(G.edges())} edges", file=sys.stderr)
    print("  running Louvain community detection...", file=sys.stderr)
    communities = detect_communities(G)
    print(f"  found {len(communities)} communities", file=sys.stderr)

    cross = cross_community_edges(G, communities)
    score_cross_community_surprise(communities, cross)

    # Build node→community map
    node_to_comm: dict[str, int] = {}
    for i, comm in enumerate(communities):
        for uid in comm:
            node_to_comm[uid] = i

    # Format community output
    comm_list = []
    for i, comm in enumerate(communities):
        members = []
        for uid in sorted(comm):
            meta = node_meta.get(uid, {})
            members.append({
                "uid": uid,
                "name": meta.get("name", uid),
                "path": meta.get("path", ""),
                "type": meta.get("type", "unknown"),
            })
        comm_list.append({"id": i, "size": len(comm), "members": members})

    # Format edges with community assignment
    edge_list = []
    for u, v, data in G.edges(data=True):
        edge_list.append({
            "source": u,
            "target": v,
            "type": data.get("type", "unknown"),
            "provenance": data.get("provenance", "extracted"),
            "confidence": data.get("confidence", 1.0),
            "community": node_to_comm.get(u, -1),
        })

    # Stats
    structural_count = sum(1 for _, _, d in G.edges(data=True) if d.get("provenance") == "extracted")
    semantic_count = sum(1 for _, _, d in G.edges(data=True) if d.get("provenance") == "inferred")

    return {
        "communities": comm_list,
        "edges": edge_list,
        "cross_edges": cross,
        "stats": {
            "total_nodes": len(G.nodes()),
            "total_edges": len(G.edges()),
            "communities": len(communities),
            "structural_edges": structural_count,
            "semantic_edges": semantic_count,
            "cross_community_edges": len(cross),
        },
    }
