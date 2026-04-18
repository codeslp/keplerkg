"""Phase 1 embed command: vectorize Function/Class nodes in KùzuDB.

Spec §6.3: kkg embed walks existing nodes, generates vectors via the
configured provider, writes them back via ALTER TABLE ... ADD embedding
FLOAT[N].  Idempotent.  --force triggers full re-vectorization.
"""

from __future__ import annotations

import sys
from typing import Any, Optional

import typer

from ..embeddings.providers import EmbeddingProvider, create_provider
from ..embeddings.runtime import (
    build_model_check_payload,
    probe_backend_support,
    resolve_embedding_config,
)
from ..embeddings.schema import (
    EMBEDDABLE_TABLES,
    EMBEDDING_COLUMN,
    ensure_embedding_columns,
    ensure_hnsw_indexes,
)
from ..io.json_stdout import emit_json
from ..io.kuzu import get_kuzu_connection

COMMAND_NAME = "embed"
SUMMARY = "Vectorize code-entity nodes in KùzuDB for hybrid retrieval."

# How many nodes to vectorize per batch round-trip.
_BATCH_SIZE = 64


def _build_embed_text(node: dict[str, Any]) -> str:
    """Build the text to vectorize for a code-entity node."""
    parts: list[str] = []
    if node.get("name"):
        parts.append(node["name"])
    if node.get("docstring"):
        parts.append(node["docstring"])
    if node.get("source"):
        parts.append(node["source"])
    return "\n".join(parts) if parts else ""


def _fetch_nodes(
    conn: Any,
    table: str,
    *,
    force: bool,
    batch_size: int,
    offset: int,
) -> list[dict[str, Any]]:
    """Fetch a batch of nodes to embed from a table."""
    where = "" if force else f"WHERE n.`{EMBEDDING_COLUMN}` IS NULL "
    query = (
        f"MATCH (n:`{table}`) {where}"
        f"RETURN n.uid AS uid, n.name AS name, n.docstring AS docstring, n.source AS source "
        f"SKIP {offset} LIMIT {batch_size}"
    )
    result = conn.execute(query)
    rows: list[dict[str, Any]] = []
    while result.has_next():
        row = result.get_next()
        rows.append({
            "uid": row[0],
            "name": row[1],
            "docstring": row[2],
            "source": row[3],
        })
    return rows


def _write_embeddings(
    conn: Any,
    table: str,
    uids: list[str],
    vectors: list[list[float]],
) -> int:
    """Write embedding vectors back to nodes.  Returns count written."""
    for uid, vec in zip(uids, vectors):
        conn.execute(
            f"MATCH (n:`{table}`) WHERE n.uid = $uid SET n.`{EMBEDDING_COLUMN}` = $vec",
            parameters={"uid": uid, "vec": vec},
        )
    return len(uids)


def _run_embed(
    conn: Any,
    provider: EmbeddingProvider,
    *,
    force: bool,
) -> dict[str, Any]:
    """Core embed loop across all embeddable tables."""
    table_stats: list[dict[str, Any]] = []
    total_embedded = 0
    total_skipped = 0

    for table in EMBEDDABLE_TABLES:
        embedded = 0
        skipped = 0
        offset = 0
        seen_uids: set[str] = set()

        while True:
            nodes = _fetch_nodes(
                conn, table, force=force, batch_size=_BATCH_SIZE, offset=offset
            )
            if not nodes:
                break

            # Deduplicate: in non-force mode, empty-text nodes stay
            # WHERE embedding IS NULL and would be re-fetched forever.
            # Track seen UIDs to break the cycle.
            new_nodes = [n for n in nodes if n["uid"] not in seen_uids]
            if not new_nodes:
                break
            for n in new_nodes:
                seen_uids.add(n["uid"])

            texts = [_build_embed_text(n) for n in new_nodes]
            embeddable_indices = [i for i, t in enumerate(texts) if t.strip()]
            skipped += len(texts) - len(embeddable_indices)

            if embeddable_indices:
                embeddable_texts = [texts[i] for i in embeddable_indices]
                vectors = provider.embed_texts(embeddable_texts)
                embeddable_uids = [new_nodes[i]["uid"] for i in embeddable_indices]
                written = _write_embeddings(conn, table, embeddable_uids, vectors)
                embedded += written
                print(
                    f"  {table}: embedded {embedded} nodes...",
                    file=sys.stderr,
                    end="\r",
                )

            # In non-force mode, successfully embedded nodes disappear
            # from the IS NULL query, but empty-text nodes persist.
            # The seen_uids set prevents infinite re-fetch.  We still
            # advance offset so the DB cursor moves forward.
            offset += _BATCH_SIZE

        print(file=sys.stderr)  # newline after \r progress
        table_stats.append({
            "table": table,
            "embedded": embedded,
            "skipped_empty": skipped,
        })
        total_embedded += embedded
        total_skipped += skipped

    return {
        "tables": table_stats,
        "total_embedded": total_embedded,
        "total_skipped_empty": total_skipped,
    }


def embed_command(
    check_model: bool = typer.Option(
        False,
        "--check-model",
        help="Validate provider/model prerequisites without mutating the graph.",
    ),
    provider: Optional[str] = typer.Option(
        None,
        "--provider",
        help="Embedding provider override (local, voyage, openai).",
    ),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        help="Embedding model id override.",
    ),
    dimensions: Optional[int] = typer.Option(
        None,
        "--dimensions",
        min=1,
        help="Embedding dimensions override.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Re-embed all nodes, even those with existing vectors.",
    ),
) -> None:
    """Vectorize code-entity nodes in KùzuDB for hybrid retrieval."""

    # --- Backend gate (unchanged from Phase 1 scaffold) ---
    backend_payload = probe_backend_support()
    if not backend_payload["ok"]:
        typer.echo(emit_json(backend_payload))
        raise typer.Exit(code=1)

    try:
        config = resolve_embedding_config(
            provider=provider,
            model=model,
            dimensions=dimensions,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    # --- Check-model path (non-mutating, unchanged) ---
    if check_model:
        payload = build_model_check_payload(config, backend=str(backend_payload["backend"]))
        typer.echo(emit_json(payload))
        raise typer.Exit(code=0 if payload["ok"] else 1)

    # --- Write path ---
    emb_provider = create_provider(config)
    conn = get_kuzu_connection()

    # Ensure schema has embedding columns
    col_results = ensure_embedding_columns(conn, config.dimensions)
    idx_results = ensure_hnsw_indexes(conn, config.dimensions)

    # Run vectorization
    print(
        f"Embedding with {config.provider}/{config.model} ({config.dimensions}d)...",
        file=sys.stderr,
    )
    embed_results = _run_embed(conn, emb_provider, force=force)

    payload = {
        "ok": True,
        "kind": "embed_complete",
        "backend": backend_payload["backend"],
        "provider": config.provider,
        "model": config.model,
        "dimensions": config.dimensions,
        "force": force,
        "schema": col_results + idx_results,
        **embed_results,
    }
    typer.echo(emit_json(payload))
    raise typer.Exit(code=0)
