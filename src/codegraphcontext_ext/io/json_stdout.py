"""Minimal JSON stdout helper for cgraph command scaffolding."""

from __future__ import annotations

import json
from typing import Any


def emit_json(payload: Any) -> str:
    """Serialize a payload the same way scaffolded commands will eventually emit it."""

    return json.dumps(payload, sort_keys=True)

