"""KùzuDB schema mutation for embedding vector columns.

Adds FLOAT[N] embedding columns to code-entity node tables and creates
HNSW indexes for ANN retrieval. Idempotent — safe to run repeatedly.

Spec §6.3: ALTER TABLE ... ADD embedding FLOAT[N]; dimensions derived
from configured model, not hardcoded.
"""

from __future__ import annotations

import sys
from typing import Any

# Node tables that get embedding columns.  Function and Class are the
# primary retrieval targets; the rest can be added post-MVP.
EMBEDDABLE_TABLES = ("Function", "Class")

EMBEDDING_COLUMN = "embedding"
NAME_EMBEDDING_COLUMN = "name_embedding"


def ensure_embedding_columns(conn: Any, dimensions: int) -> list[dict[str, object]]:
    """Add embedding FLOAT[N] columns to embeddable node tables.

    Returns a list of per-table result dicts:
      {"table": str, "action": "created"|"exists"|"error", "detail": str}
    """
    results: list[dict[str, object]] = []
    for table in EMBEDDABLE_TABLES:
        try:
            conn.execute(
                f"ALTER TABLE `{table}` ADD `{EMBEDDING_COLUMN}` FLOAT[{dimensions}]"
            )
            results.append({
                "table": table,
                "action": "created",
                "detail": f"Added {EMBEDDING_COLUMN} FLOAT[{dimensions}]",
            })
            print(f"Added {EMBEDDING_COLUMN} column to {table}", file=sys.stderr)
        except Exception as exc:
            msg = str(exc).lower()
            if "already exists" in msg or "exist" in msg:
                results.append({
                    "table": table,
                    "action": "exists",
                    "detail": f"{EMBEDDING_COLUMN} column already present",
                })
            else:
                results.append({
                    "table": table,
                    "action": "error",
                    "detail": str(exc),
                })
                print(
                    f"Warning: could not add {EMBEDDING_COLUMN} to {table}: {exc}",
                    file=sys.stderr,
                )
    return results


def ensure_name_embedding_columns(conn: Any, dimensions: int) -> list[dict[str, object]]:
    """Add name_embedding FLOAT[N] columns to embeddable node tables.

    Mirrors ensure_embedding_columns for the name-only embedding used by
    naming-analysis standards (CGQ-F01 through F04).
    """
    results: list[dict[str, object]] = []
    for table in EMBEDDABLE_TABLES:
        try:
            conn.execute(
                f"ALTER TABLE `{table}` ADD `{NAME_EMBEDDING_COLUMN}` FLOAT[{dimensions}]"
            )
            results.append({
                "table": table,
                "action": "created",
                "detail": f"Added {NAME_EMBEDDING_COLUMN} FLOAT[{dimensions}]",
            })
            print(f"Added {NAME_EMBEDDING_COLUMN} column to {table}", file=sys.stderr)
        except Exception as exc:
            msg = str(exc).lower()
            if "already exists" in msg or "exist" in msg:
                results.append({
                    "table": table,
                    "action": "exists",
                    "detail": f"{NAME_EMBEDDING_COLUMN} column already present",
                })
            else:
                results.append({
                    "table": table,
                    "action": "error",
                    "detail": str(exc),
                })
                print(
                    f"Warning: could not add {NAME_EMBEDDING_COLUMN} to {table}: {exc}",
                    file=sys.stderr,
                )
    return results


def ensure_hnsw_indexes(conn: Any, dimensions: int) -> list[dict[str, object]]:
    """Create HNSW indexes on embedding columns for ANN search.

    Returns a list of per-table result dicts similar to ensure_embedding_columns.
    """
    results: list[dict[str, object]] = []
    for table in EMBEDDABLE_TABLES:
        index_name = f"{table.lower()}_embedding_hnsw"
        try:
            conn.execute(
                f"CREATE HNSW INDEX `{index_name}` ON `{table}` (`{EMBEDDING_COLUMN}`)"
            )
            results.append({
                "table": table,
                "action": "created",
                "detail": f"HNSW index {index_name} created",
            })
            print(f"Created HNSW index {index_name}", file=sys.stderr)
        except Exception as exc:
            msg = str(exc).lower()
            if "already exists" in msg or "exist" in msg:
                results.append({
                    "table": table,
                    "action": "exists",
                    "detail": f"HNSW index {index_name} already present",
                })
            else:
                results.append({
                    "table": table,
                    "action": "error",
                    "detail": str(exc),
                })
                print(
                    f"Warning: could not create HNSW index on {table}: {exc}",
                    file=sys.stderr,
                )
    return results


def ensure_name_hnsw_indexes(conn: Any, dimensions: int) -> list[dict[str, object]]:
    """Create HNSW indexes on name_embedding columns for ANN search."""
    results: list[dict[str, object]] = []
    for table in EMBEDDABLE_TABLES:
        index_name = f"{table.lower()}_name_embedding_hnsw"
        try:
            conn.execute(
                f"CREATE HNSW INDEX `{index_name}` ON `{table}` (`{NAME_EMBEDDING_COLUMN}`)"
            )
            results.append({
                "table": table,
                "action": "created",
                "detail": f"HNSW index {index_name} created",
            })
            print(f"Created HNSW index {index_name}", file=sys.stderr)
        except Exception as exc:
            msg = str(exc).lower()
            if "already exists" in msg or "exist" in msg:
                results.append({
                    "table": table,
                    "action": "exists",
                    "detail": f"HNSW index {index_name} already present",
                })
            else:
                results.append({
                    "table": table,
                    "action": "error",
                    "detail": str(exc),
                })
                print(
                    f"Warning: could not create HNSW index on {table}: {exc}",
                    file=sys.stderr,
                )
    return results
