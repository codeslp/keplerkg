"""Extension-registration seam for cgraph-specific CLI additions."""

from __future__ import annotations

import typer

from .commands.blast_radius import SUMMARY as BLAST_RADIUS_SUMMARY, blast_radius_command
from .commands.context import SUMMARY as CONTEXT_SUMMARY, context_command
from .commands.embed import SUMMARY as EMBED_SUMMARY, embed_command
from .commands.export_embeddings import (
    SUMMARY as EXPORT_EMB_SUMMARY,
    export_embeddings_command,
)
from .commands.review_packet import SUMMARY as REVIEW_PACKET_SUMMARY, review_packet_command
from .commands.sync_check import SUMMARY as SYNC_CHECK_SUMMARY, sync_check_command
from .commands.viz_dashboard import SUMMARY as VIZ_DASHBOARD_SUMMARY, viz_dashboard_command
from .commands.viz_embeddings import SUMMARY as VIZ_EMB_SUMMARY, viz_embeddings_command
from .commands.viz_graph import SUMMARY as VIZ_GRAPH_SUMMARY, viz_graph_command
from .commands.viz_projector import SUMMARY as VIZ_PROJECTOR_SUMMARY, viz_projector_command


def register_extensions(app: typer.Typer) -> None:
    """Register cgraph extension commands on the upstream Typer app."""

    app.command(name="blast-radius", help=BLAST_RADIUS_SUMMARY)(blast_radius_command)
    app.command(name="sync-check", help=SYNC_CHECK_SUMMARY)(sync_check_command)
    app.command(name="embed", help=EMBED_SUMMARY)(embed_command)
    app.command(name="search", help=CONTEXT_SUMMARY)(context_command)
    app.command(name="review-packet", help=REVIEW_PACKET_SUMMARY)(review_packet_command)
    app.command(name="viz-embeddings", help=VIZ_EMB_SUMMARY)(viz_embeddings_command)
    app.command(name="viz-graph", help=VIZ_GRAPH_SUMMARY)(viz_graph_command)
    app.command(name="viz-dashboard", help=VIZ_DASHBOARD_SUMMARY)(viz_dashboard_command)
    app.command(name="viz-projector", help=VIZ_PROJECTOR_SUMMARY)(viz_projector_command)
    app.command(name="export-embeddings", help=EXPORT_EMB_SUMMARY)(export_embeddings_command)
