"""JSON stdout helpers for cgraph commands.

Provides the canonical envelope wrapper (``make_envelope``) that every
cgraph command should use to construct its output, plus the low-level
``emit_json`` serializer.

Envelope contract (Phase 2.5):
    Every JSON response carries these top-level fields:
    - ``ok``             bool    — whether the command succeeded
    - ``kind``           str     — discriminator matching the command name
    - ``schema_version`` str     — envelope version (currently "1.0")
    - ``project``        str|null — active project slug or null
    Command-specific fields are merged at the same top level.
"""

from __future__ import annotations

import json
import os
from typing import Any

SCHEMA_VERSION = "1.0"

_ENVELOPE_KEYS = frozenset({"ok", "kind", "schema_version", "project", "error"})

_UNSET: str | None = object()  # type: ignore[assignment]  # sentinel


def make_envelope(
    kind: str,
    data: dict[str, Any] | None = None,
    *,
    ok: bool = True,
    error: str | None = None,
    project: str | None = _UNSET,
) -> dict[str, Any]:
    """Build the canonical cgraph JSON envelope.

    Envelope fields (``ok``, ``kind``, ``schema_version``, ``project``)
    are always present.  Command-specific *data* is merged at the top
    level so existing consumers see their fields unchanged.

    *project* is the resolved slug from ``activate_project()``.  When
    omitted, falls back to ``$CGRAPH_PROJECT`` for commands that haven't
    migrated yet.  Callers that already have a ``ProjectTarget`` should
    pass ``project=target.slug`` explicitly.

    Raises ``ValueError`` if *data* attempts to overwrite a reserved
    envelope key (``ok``, ``kind``, ``schema_version``, ``project``,
    ``error``).
    """
    resolved_project = (
        os.environ.get("CGRAPH_PROJECT") if project is _UNSET else project
    )
    envelope: dict[str, Any] = {
        "ok": ok,
        "kind": kind,
        "schema_version": SCHEMA_VERSION,
        "project": resolved_project,
    }
    if data:
        collisions = _ENVELOPE_KEYS & data.keys()
        if collisions:
            raise ValueError(
                f"Command data must not overwrite reserved envelope "
                f"key(s): {', '.join(sorted(collisions))}"
            )
        envelope.update(data)
    if error is not None:
        envelope["error"] = error
    return envelope


def emit_json(payload: Any) -> str:
    """Serialize a payload to a single JSON line on stdout."""

    return json.dumps(payload, sort_keys=True)

