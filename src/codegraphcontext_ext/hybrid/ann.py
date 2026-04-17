"""ANN vector search over KùzuDB HNSW-indexed embedding columns.

Given a query string, embeds it via the configured provider and runs
approximate nearest-neighbor search on Function/Class node embeddings.
Returns scored seed nodes for the context command.
"""

from __future__ import annotations

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
        index_name = f"{table.lower()}_embedding_hnsw"
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
            while result.has_next():
                row = result.get_next()
                # Distance → similarity score (lower distance = higher score)
                distance = row[4] if row[4] is not None else float("inf")
                score = 1.0 / (1.0 + distance)
                all_results.append({
                    "uid": row[0],
                    "name": row[1],
                    "file": f"{row[2]}:{row[3]}" if row[2] and row[3] else row[2],
                    "table": table,
                    "score": round(score, 4),
                })
        except Exception:
            # Table may not have embeddings or HNSW index yet — skip silently
            pass

    # Sort by score descending, take top-k across all tables
    all_results.sort(key=lambda r: r["score"], reverse=True)
    return all_results[:k]
