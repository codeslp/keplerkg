"""Extension-registration seam for cgraph-specific CLI additions."""

from __future__ import annotations

import typer

from .commands.advise import SUMMARY as ADVISE_SUMMARY, advise_command
from .commands.audit import SUMMARY as AUDIT_SUMMARY, audit_command
from .commands.blast_radius import SUMMARY as BLAST_RADIUS_SUMMARY, blast_radius_command
from .commands.clusters import SUMMARY as CLUSTERS_SUMMARY, clusters_command
from .commands.context import SUMMARY as CONTEXT_SUMMARY, context_command
from .commands.doctor import SUMMARY as DOCTOR_SUMMARY, doctor_command
from .commands.drift_check import SUMMARY as DRIFT_CHECK_SUMMARY, drift_check_command
from .commands.embed import SUMMARY as EMBED_SUMMARY, embed_command
from .commands.health import SUMMARY as HEALTH_SUMMARY, health_command
from .commands.hotspots import SUMMARY as HOTSPOTS_SUMMARY, hotspots_command
from .commands.entrypoints import SUMMARY as ENTRYPOINTS_SUMMARY, entrypoints_command
from .commands.execution_flow import (
    SUMMARY as EXEC_FLOW_SUMMARY,
    execution_flow_command,
)
from .commands.export_embeddings import (
    SUMMARY as EXPORT_EMB_SUMMARY,
    export_embeddings_command,
)
from .commands.impact import SUMMARY as IMPACT_SUMMARY, impact_command
from .commands.manifest import SUMMARY as MANIFEST_SUMMARY, manifest_command
from .commands.repl import SUMMARY as REPL_SUMMARY, repl_command
from .commands.review_packet import SUMMARY as REVIEW_PACKET_SUMMARY, review_packet_command
from .commands.snapshot import SUMMARY as SNAPSHOT_SUMMARY, snapshot_command
from .commands.sync_check import SUMMARY as SYNC_CHECK_SUMMARY, sync_check_command
from .commands.viz_dashboard import SUMMARY as VIZ_DASHBOARD_SUMMARY, viz_dashboard_command
from .commands.viz_embeddings import SUMMARY as VIZ_EMB_SUMMARY, viz_embeddings_command
from .commands.viz_graph import SUMMARY as VIZ_GRAPH_SUMMARY, viz_graph_command
from .commands.viz_projector import SUMMARY as VIZ_PROJECTOR_SUMMARY, viz_projector_command
from .daemon.serve import (
    LOCALHOST_SUMMARY,
    SUMMARY as SERVE_SUMMARY,
    serve_command,
    serve_localhost_command,
)


def register_extensions(app: typer.Typer) -> None:
    """Register cgraph extension commands on the upstream Typer app."""

    app.command(name="advise", help=ADVISE_SUMMARY)(advise_command)
    app.command(name="audit", help=AUDIT_SUMMARY)(audit_command)
    app.command(name="blast-radius", help=BLAST_RADIUS_SUMMARY)(blast_radius_command)
    app.command(name="clusters", help=CLUSTERS_SUMMARY)(clusters_command)
    app.command(name="doctor", help=DOCTOR_SUMMARY)(doctor_command)
    app.command(name="drift-check", help=DRIFT_CHECK_SUMMARY)(drift_check_command)
    app.command(name="sync-check", help=SYNC_CHECK_SUMMARY)(sync_check_command)
    app.command(name="embed", help=EMBED_SUMMARY)(embed_command)
    app.command(name="health", help=HEALTH_SUMMARY)(health_command)
    app.command(name="hotspots", help=HOTSPOTS_SUMMARY)(hotspots_command)
    app.command(name="entrypoints", help=ENTRYPOINTS_SUMMARY)(entrypoints_command)
    app.command(name="execution-flow", help=EXEC_FLOW_SUMMARY)(execution_flow_command)
    app.command(name="impact", help=IMPACT_SUMMARY)(impact_command)
    app.command(name="manifest", help=MANIFEST_SUMMARY)(manifest_command)
    app.command(name="repl", help=REPL_SUMMARY)(repl_command)
    app.command(name="search", help=CONTEXT_SUMMARY)(context_command)
    app.command(name="review-packet", help=REVIEW_PACKET_SUMMARY)(review_packet_command)
    app.command(name="snapshot", help=SNAPSHOT_SUMMARY)(snapshot_command)
    app.command(name="viz-embeddings", help=VIZ_EMB_SUMMARY)(viz_embeddings_command)
    app.command(name="viz-graph", help=VIZ_GRAPH_SUMMARY)(viz_graph_command)
    app.command(name="viz-dashboard", help=VIZ_DASHBOARD_SUMMARY)(viz_dashboard_command)
    app.command(name="viz-projector", help=VIZ_PROJECTOR_SUMMARY)(viz_projector_command)
    app.command(name="export-embeddings", help=EXPORT_EMB_SUMMARY)(export_embeddings_command)
    app.command(name="serve", help=SERVE_SUMMARY)(serve_command)
    app.command(name="serve-localhost", help=LOCALHOST_SUMMARY)(serve_localhost_command)
