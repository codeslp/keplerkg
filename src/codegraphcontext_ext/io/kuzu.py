"""Active embedded-backend graph-connection accessor.

Name is historical — this accessor returns Kuzu's native connection for
KùzuDB and a Kuzu-compatible shim for FalkorDB. Rename is a Phase G
follow-up (Spec 006).
"""

from __future__ import annotations

from typing import Any


def get_kuzu_connection() -> Any:
    """Return a Kuzu-style ``conn`` for the currently active local backend.

    Runs the storage preflight first so we never silently recreate data
    on the internal disk when ``/Volumes/zombie`` is unmounted. Imports
    are local so test suites that never need a live DB don't pay the
    upstream import cost. For FalkorDB, returns a
    ``FalkorDBKuzuCompatConnection``; backend-specific syntax
    (``ALTER TABLE``, ``CREATE HNSW INDEX``, ``CALL hnsw_search``) still
    requires callers to branch — the shim does not translate DDL.
    """
    from ..embeddings.runtime import active_local_backend
    from ..preflight import require_storage

    require_storage()

    if active_local_backend() == "falkordb":
        from codegraphcontext.core.database_falkordb import FalkorDBManager

        return FalkorDBManager().get_conn()

    from codegraphcontext.core.database_kuzu import KuzuDBManager

    manager = KuzuDBManager()
    driver = manager.get_driver()
    return driver.conn
