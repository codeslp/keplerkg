"""Single-file wrapping layer over upstream ``codegraphcontext.core`` helpers.

Spec §8 prefers subclassing/wrapping over in-place edits of upstream-owned
code. cgraph's backend probe needs a handful of availability/configuration
helpers from ``codegraphcontext.core`` that upstream exposes as leading-
underscore private API. Keeping those imports centralised here means:

- One clear error point if an upstream sync renames or removes a helper.
- Call sites elsewhere in ``codegraphcontext_ext`` reference public names
  (``is_kuzudb_available`` etc.) that do not change with upstream churn.
- ``tests/cgraph_ext/test_upstream_coupling.py`` trips on the same private
  names, so a rename surfaces as a failing test in CI instead of a confusing
  ``AttributeError`` at runtime.

Imports of ``codegraphcontext.core`` are intentionally lazy: the upstream
package pulls in heavy dependencies (e.g. the neo4j driver) that are not
available in every dev environment.  Importing this module on its own does
**not** trigger the upstream import chain.
"""

from __future__ import annotations

from typing import Callable

_UPSTREAM_HELPER_NAMES = {
    "is_kuzudb_available": "_is_kuzudb_available",
    "is_falkordb_available": "_is_falkordb_available",
    "is_falkordb_remote_configured": "_is_falkordb_remote_configured",
    "is_neo4j_configured": "_is_neo4j_configured",
}


def _load(upstream_name: str) -> Callable[[], bool]:
    """Resolve a private upstream helper, raising a clear error on rename."""

    from codegraphcontext import core  # imported lazily; see module docstring

    helper = getattr(core, upstream_name, None)
    if not callable(helper):
        raise ImportError(
            f"codegraphcontext.core.{upstream_name} is missing or not callable. "
            "cgraph's backend probe depends on it — an upstream sync likely "
            "renamed or removed it. Update "
            "src/codegraphcontext_ext/embeddings/_upstream.py and the matching "
            "tripwire in tests/cgraph_ext/test_upstream_coupling.py."
        )
    return helper


def is_kuzudb_available() -> bool:
    """Return whether KùzuDB is importable (proxies upstream)."""

    return _load(_UPSTREAM_HELPER_NAMES["is_kuzudb_available"])()


def is_falkordb_available() -> bool:
    """Return whether FalkorDB Lite is importable (proxies upstream)."""

    return _load(_UPSTREAM_HELPER_NAMES["is_falkordb_available"])()


def is_falkordb_remote_configured() -> bool:
    """Return whether a remote FalkorDB host is configured (proxies upstream)."""

    return _load(_UPSTREAM_HELPER_NAMES["is_falkordb_remote_configured"])()


def is_neo4j_configured() -> bool:
    """Return whether Neo4j has credentials configured (proxies upstream)."""

    return _load(_UPSTREAM_HELPER_NAMES["is_neo4j_configured"])()
