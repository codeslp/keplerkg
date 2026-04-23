"""Schema mutation for embedding vector columns.

On KùzuDB this adds ``FLOAT[N]`` columns to code-entity node tables via
``ALTER TABLE`` and creates HNSW indexes for ANN retrieval; both are
idempotent. On FalkorDB the graph is schemaless, so ALTER is a no-op —
the embedding property materializes when ``SET n.embedding = $vec`` runs
during the embed write loop. HNSW is Kuzu-specific; on FalkorDB we let
ANN search fall back to the linear-scan path in ``hybrid/ann.py``.

Spec §6.3: ALTER TABLE ... ADD embedding FLOAT[N]; dimensions derived
from configured model, not hardcoded.
"""

from __future__ import annotations

import sys
from typing import Any

from .runtime import active_local_backend

# Node tables that get embedding columns.  Function and Class are the
# primary retrieval targets; the rest can be added post-MVP.
EMBEDDABLE_TABLES = ("Function", "Class")

EMBEDDING_COLUMN = "embedding"
NAME_EMBEDDING_COLUMN = "name_embedding"


def _schemaless_results(column: str, reason: str) -> list[dict[str, object]]:
    """Build the 'skipped_on_backend' payload for schemaless backends."""
    return [
        {
            "table": table,
            "action": "skipped_on_backend",
            "detail": f"{reason} ({column} on {table})",
        }
        for table in EMBEDDABLE_TABLES
    ]


def ensure_embedding_columns(conn: Any, dimensions: int) -> list[dict[str, object]]:
    """Add embedding FLOAT[N] columns to embeddable node tables.

    Returns a list of per-table result dicts:
      {"table": str, "action": "created"|"exists"|"error"|"skipped_on_backend",
       "detail": str}

    FalkorDB is schemaless, so the column materializes when the embed
    loop executes ``SET n.embedding = $vec``. We still emit a per-table
    status row so the JSON payload shape stays stable across backends.
    """
    if active_local_backend() == "falkordb":
        return _schemaless_results(
            EMBEDDING_COLUMN,
            "FalkorDB is schemaless; column materializes on SET",
        )

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
    if active_local_backend() == "falkordb":
        return _schemaless_results(
            NAME_EMBEDDING_COLUMN,
            "FalkorDB is schemaless; column materializes on SET",
        )

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

    Kuzu-only. FalkorDB's vector-index syntax differs (no ``CREATE HNSW
    INDEX``), so we skip index creation there and rely on the linear-scan
    fallback in ``hybrid/ann.py`` for ANN queries.
    """
    if active_local_backend() == "falkordb":
        return _schemaless_results(
            "hnsw",
            "HNSW index is Kuzu-specific; FalkorDB uses the linear-scan fallback in hybrid/ann.py",
        )

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
    """Create HNSW indexes on name_embedding columns for ANN search.

    Kuzu-only. FalkorDB skips; see ``ensure_hnsw_indexes``.
    """
    if active_local_backend() == "falkordb":
        return _schemaless_results(
            "hnsw (name)",
            "HNSW name index is Kuzu-specific; FalkorDB uses the linear-scan fallback in hybrid/ann.py",
        )

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
