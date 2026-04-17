"""Extension-registration seam for cgraph-specific CLI additions."""

from __future__ import annotations

import typer

from .commands.context import SUMMARY as CONTEXT_SUMMARY, context_command
from .commands.embed import SUMMARY as EMBED_SUMMARY, embed_command
from .commands.review_packet import SUMMARY as REVIEW_PACKET_SUMMARY, review_packet_command
from .commands.sync_check import SUMMARY as SYNC_CHECK_SUMMARY, sync_check_command
from .commands.viz_embeddings import SUMMARY as VIZ_EMB_SUMMARY, viz_embeddings_command
from .commands.viz_graph import SUMMARY as VIZ_GRAPH_SUMMARY, viz_graph_command


def register_extensions(app: typer.Typer) -> None:
    """Register cgraph extension commands on the upstream Typer app."""

    app.command(name="sync-check", help=SYNC_CHECK_SUMMARY)(sync_check_command)
    app.command(name="embed", help=EMBED_SUMMARY)(embed_command)
    app.command(name="context", help=CONTEXT_SUMMARY)(context_command)
    app.command(name="review-packet", help=REVIEW_PACKET_SUMMARY)(review_packet_command)
    app.command(name="viz-embeddings", help=VIZ_EMB_SUMMARY)(viz_embeddings_command)
    app.command(name="viz-graph", help=VIZ_GRAPH_SUMMARY)(viz_graph_command)
