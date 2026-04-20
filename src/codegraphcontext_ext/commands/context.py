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
from ..hybrid.ann import search as ann_search, search_scoped
from ..hybrid.traverse import traverse
from ..io.json_stdout import emit_json
from ..io.kuzu import get_kuzu_connection
from ..project import PROJECT_OPTION_HELP, activate_project

COMMAND_NAME = "search"
SCHEMA_FILE = "context.json"
SUMMARY = "Semantic search: ANN vector search + graph neighborhood expansion."


def _resolve_community_uids(conn: Any, community_id: int) -> set[str]:
    """Get UIDs belonging to a Louvain community (0-indexed)."""
    from ..topology.communities import build_combined_graph, detect_communities
    G, _ = build_combined_graph(conn, include_semantic=False)
    communities = detect_communities(G)
    if 0 <= community_id < len(communities):
        return communities[community_id]
    return set()


def _resolve_cluster_uids(conn: Any, cluster_id: int) -> set[str]:
    """Get UIDs belonging to an HDBSCAN cluster."""
    from ..topology.hdbscan_overlay import get_cluster_uids
    return get_cluster_uids(conn, cluster_id)


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
    mode: str = typer.Option(
        "global",
        "--mode",
        help="Search mode: global (all embeddings), community (Louvain), cluster (HDBSCAN).",
    ),
    community_id: Optional[int] = typer.Option(
        None,
        "--community-id",
        help="Louvain community ID to scope results to (requires --mode community).",
    ),
    cluster_id: Optional[int] = typer.Option(
        None,
        "--cluster-id",
        help="HDBSCAN cluster ID to scope results to (requires --mode cluster).",
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
    project: Optional[str] = typer.Option(
        None,
        "--project",
        help=PROJECT_OPTION_HELP,
    ),
) -> None:
    """Hybrid retrieval: find code relevant to a query via ANN + graph walk."""
    activate_project(project)

    # Validate --mode and required/incompatible companion flags
    valid_modes = ("global", "community", "cluster")
    if mode not in valid_modes:
        raise typer.BadParameter(
            f"--mode must be one of {', '.join(valid_modes)}, got '{mode}'."
        )
    if mode == "global" and (community_id is not None or cluster_id is not None):
        raise typer.BadParameter("--mode global does not accept --community-id or --cluster-id.")
    if mode == "community":
        if community_id is None:
            raise typer.BadParameter("--mode community requires --community-id.")
        if cluster_id is not None:
            raise typer.BadParameter("--mode community does not accept --cluster-id.")
    if mode == "cluster":
        if cluster_id is None:
            raise typer.BadParameter("--mode cluster requires --cluster-id.")
        if community_id is not None:
            raise typer.BadParameter("--mode cluster does not accept --community-id.")

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

    if mode == "community" and community_id is not None:
        allowed = _resolve_community_uids(conn, community_id)
        seeds = search_scoped(conn, query_vector, k=k, allowed_uids=allowed)
    elif mode == "cluster" and cluster_id is not None:
        allowed = _resolve_cluster_uids(conn, cluster_id)
        seeds = search_scoped(conn, query_vector, k=k, allowed_uids=allowed)
    else:
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
