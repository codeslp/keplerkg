"""Shared accessor for the upstream KùzuDB connection singleton."""

from __future__ import annotations

from typing import Any


def get_kuzu_connection() -> Any:
    """Return a raw kuzu.Connection from upstream's singleton manager.

    Runs the Phase 1.5 Step 7 storage preflight before touching KùzuDB
    so we never silently recreate the store on internal when zombie is
    unmounted.

    Import is local so test suites that never need a live DB don't pay the
    upstream import cost.
    """
    from ..preflight import require_storage

    require_storage()

    from codegraphcontext.core.database_kuzu import KuzuDBManager

    manager = KuzuDBManager()
    driver = manager.get_driver()
    return driver.conn
