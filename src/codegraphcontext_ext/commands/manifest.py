"""kkg manifest — self-describing command registry for agent tooling.

Phase 2.5: agents need to discover which commands exist, what schemas
they use, whether they accept ``--project``, and whether they touch
KuzuDB, without parsing ``--help`` text.
"""

from __future__ import annotations

from typing import Optional

import typer

from ..io.json_stdout import emit_json, make_envelope
from ..io.registry import get_command_registry

SUMMARY = "Emit a machine-readable manifest of all kkg commands."


def manifest_command(
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Emit JSON output (default and only supported format).",
    ),
    fmt: Optional[str] = typer.Option(
        "json",
        "--format",
        help="Output format (only 'json' is supported).",
    ),
) -> None:
    """Emit a machine-readable manifest of all kkg commands."""
    if fmt != "json":
        raise typer.BadParameter(
            f"Unsupported format {fmt!r}; only 'json' is supported.",
            param_hint="--format",
        )
    registry = get_command_registry()
    payload = make_envelope(
        "manifest",
        {
            "commands": registry,
            "envelope_schema": "envelope.json",
            "total_commands": len(registry),
        },
    )
    typer.echo(emit_json(payload))
    raise typer.Exit(code=0)
