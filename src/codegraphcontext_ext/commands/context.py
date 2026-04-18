"""Phase 1 context command: hybrid retrieval via ANN + graph traversal.

Spec §4.2: kkg context <query> runs ANN vector search for top-k
semantically relevant Function/Class nodes, then traverses depth hops
of CALLS/IMPORTS edges.  Emits JSON with seeds, neighborhood, and
token estimate.
"""

from __future__ import annotations

import sys
from typing import Any, Optional

import typer

from ..embeddings.providers import create_provider
from ..embeddings.runtime import (
    probe_backend_support,
    resolve_embedding_config,
)
from ..hybrid.ann import search as ann_search
from ..hybrid.traverse import traverse
from ..io.json_stdout import emit_json
from ..io.kuzu import get_kuzu_connection

COMMAND_NAME = "context"
SCHEMA_FILE = "context.json"
SUMMARY = "Hybrid retrieval: ANN vector search + graph traversal."


def _estimate_tokens(text: str) -> int:
    """Rough token estimate using cl100k_base heuristic (~4 chars/token)."""
    return max(1, len(text) // 4)


def _build_context_payload(
    query: str,
    seeds: list[dict[str, Any]],
    neighborhood: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Assemble the spec-defined JSON output shape."""
    # Estimate tokens for the whole payload
    import json
    payload_str = json.dumps({"seeds": seeds, "neighborhood": neighborhood})
    token_est = _estimate_tokens(payload_str)

    return {
        "query": query,
        "seeds": seeds,
        "neighborhood": neighborhood,
        "token_estimate": token_est,
        "token_estimate_basis": "cl100k_base (approximate; consumers should re-tokenize with their own model)",
    }


def context_command(
    query: str = typer.Argument(
        ...,
        help="Natural-language query describing the code area of interest.",
    ),
    lane: Optional[str] = typer.Option(
        None,
        "--lane",
        help="btrain lane id (reserved for future lane-scoped filtering).",
    ),
    k: int = typer.Option(
        8,
        "--k",
        min=1,
        help="Number of top ANN results to retrieve.",
    ),
    depth: int = typer.Option(
        1,
        "--depth",
        min=0,
        help="Number of graph hops to traverse from seed nodes.",
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
) -> None:
    """Hybrid retrieval: find code relevant to a query via ANN + graph walk."""

    # Backend gate
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

    # Embed the query
    emb_provider = create_provider(config)
    print(f"Embedding query with {config.provider}...", file=sys.stderr)
    query_vectors = emb_provider.embed_texts([query])
    query_vector = query_vectors[0]

    # Connect and search
    conn = get_kuzu_connection()

    seeds = ann_search(conn, query_vector, k=k)

    if not seeds:
        payload = _build_context_payload(query, [], {"callers": [], "callees": [], "imports": []})
        typer.echo(emit_json(payload))
        raise typer.Exit(code=0)

    # Traverse from seeds
    seed_uids = [s["uid"] for s in seeds]
    neighborhood = traverse(conn, seed_uids, depth=depth)

    payload = _build_context_payload(query, seeds, neighborhood)
    typer.echo(emit_json(payload))
    raise typer.Exit(code=0)
