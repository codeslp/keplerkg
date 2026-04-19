"""ANN vector search over KùzuDB HNSW-indexed embedding columns.

Given a query string, embeds it via the configured provider and runs
approximate nearest-neighbor search on Function/Class node embeddings.
Returns scored seed nodes for the context command.
"""

from __future__ import annotations

import math
from typing import Any

from ..embeddings.schema import EMBEDDABLE_TABLES, EMBEDDING_COLUMN


def search(
    conn: Any,
    query_vector: list[float],
    *,
    k: int = 8,
    tables: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Run ANN search across embeddable tables, return top-k scored seeds.

    Each result dict: {uid, name, file, table, score}.
    Results are merged across tables and sorted by score descending.
    """
    target_tables = tables or EMBEDDABLE_TABLES
    all_results: list[dict[str, Any]] = []

    for table in target_tables:
        query = (
            f"CALL hnsw_search(`{table}`, `{EMBEDDING_COLUMN}`, "
            f"$query_vec, {k}) "
            f"YIELD node, distance "
            f"RETURN node.uid AS uid, node.name AS name, "
            f"node.path AS file, node.line_number AS line_number, "
            f"distance "
            f"LIMIT {k}"
        )
        try:
            result = conn.execute(query, parameters={"query_vec": query_vector})
            all_results.extend(_rows_to_results(result, table))
            continue
        except Exception:
            # New project stores can be searchable before HNSW is available on
            # the installed Kùzu build. Fall back to a linear embedding scan.
            fallback = _linear_scan(conn, table, query_vector, k=k)
            all_results.extend(fallback)

    # Sort by score descending, take top-k across all tables
    all_results.sort(key=lambda r: r["score"], reverse=True)
    return all_results[:k]


def _rows_to_results(result: Any, table: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    while result.has_next():
        row = result.get_next()
        distance = row[4] if row[4] is not None else float("inf")
        score = 1.0 / (1.0 + distance)
        rows.append({
            "uid": row[0],
            "name": row[1],
            "file": f"{row[2]}:{row[3]}" if row[2] and row[3] else row[2],
            "table": table,
            "score": round(score, 4),
        })
    return rows


def _linear_scan(
    conn: Any,
    table: str,
    query_vector: list[float],
    *,
    k: int,
) -> list[dict[str, Any]]:
    query = (
        f"MATCH (n:`{table}`) "
        f"WHERE n.`{EMBEDDING_COLUMN}` IS NOT NULL "
        f"RETURN n.uid AS uid, n.name AS name, "
        f"n.path AS file, n.line_number AS line_number, "
        f"n.`{EMBEDDING_COLUMN}` AS embedding"
    )
    try:
        result = conn.execute(query)
    except Exception:
        return []

    scored: list[dict[str, Any]] = []
    while result.has_next():
        row = result.get_next()
        embedding = row[4]
        if not embedding:
            continue
        distance = _l2_distance(query_vector, embedding)
        score = 1.0 / (1.0 + distance)
        scored.append({
            "uid": row[0],
            "name": row[1],
            "file": f"{row[2]}:{row[3]}" if row[2] and row[3] else row[2],
            "table": table,
            "score": round(score, 4),
        })

    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[:k]


def _l2_distance(left: list[float], right: list[float]) -> float:
    dims = min(len(left), len(right))
    if dims == 0:
        return float("inf")
    return math.sqrt(sum((left[i] - right[i]) ** 2 for i in range(dims)))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity in [-1, 1].  Returns 0.0 on degenerate input."""
    dims = min(len(a), len(b))
    if dims == 0:
        return 0.0
    dot = sum(a[i] * b[i] for i in range(dims))
    norm_a = math.sqrt(sum(x * x for x in a[:dims]))
    norm_b = math.sqrt(sum(x * x for x in b[:dims]))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
