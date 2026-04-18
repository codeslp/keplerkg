"""Shared helper to read embedded Function/Class nodes from KùzuDB."""

from __future__ import annotations

from typing import Any

from .schema import EMBEDDABLE_TABLES, EMBEDDING_COLUMN


def fetch_embedded_nodes(conn: Any) -> list[dict[str, Any]]:
    """Fetch all nodes across EMBEDDABLE_TABLES that carry an embedding vector.

    Returns dicts with uid/name/path/line/embedding/type.  Per-table errors are
    swallowed: a missing column surfaces upstream as an empty result, not a
    traceback.
    """
    nodes: list[dict[str, Any]] = []
    for table in EMBEDDABLE_TABLES:
        query = (
            f"MATCH (n:`{table}`) "
            f"WHERE n.`{EMBEDDING_COLUMN}` IS NOT NULL "
            f"RETURN n.uid AS uid, n.name AS name, n.path AS path, "
            f"n.line_number AS line, n.`{EMBEDDING_COLUMN}` AS embedding"
        )
        try:
            result = conn.execute(query)
            while result.has_next():
                row = result.get_next()
                nodes.append({
                    "uid": row[0],
                    "name": row[1] or "(anonymous)",
                    "path": row[2] or "",
                    "line": row[3],
                    "embedding": list(row[4]),
                    "type": table,
                })
        except Exception:
            pass
    return nodes
