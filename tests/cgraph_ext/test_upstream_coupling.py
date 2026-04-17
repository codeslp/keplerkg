"""Tripwire tests for private upstream helpers cgraph depends on.

cgraph's backend probe reaches into ``codegraphcontext.core`` for availability
and configuration checks (``_is_kuzudb_available``, ``_is_falkordb_available``,
``_is_falkordb_remote_configured``, ``_is_neo4j_configured``). Spec §8 says we
should prefer subclassing/wrapping over importing private API, but for now we
proxy these helpers directly. This test fails loudly on the next upstream sync
that renames or removes any of them so we notice before runtime breakage.
"""

from __future__ import annotations

import pytest

REQUIRED_UPSTREAM_HELPERS = (
    "_is_kuzudb_available",
    "_is_falkordb_available",
    "_is_falkordb_remote_configured",
    "_is_neo4j_configured",
)


@pytest.fixture(scope="module")
def upstream_core():
    # Skip when the environment doesn't have the full upstream runtime installed
    # (e.g. no neo4j driver). CI installs the project with its deps, so the
    # tripwire still fires there on a real upstream rename/remove.
    return pytest.importorskip(
        "codegraphcontext.core",
        reason="Upstream dependencies not installed; tripwire runs in CI.",
    )


@pytest.mark.parametrize("helper_name", REQUIRED_UPSTREAM_HELPERS)
def test_upstream_backend_helper_still_exists(upstream_core, helper_name):
    helper = getattr(upstream_core, helper_name, None)
    assert callable(helper), (
        f"codegraphcontext.core.{helper_name} is missing or not callable. "
        "cgraph's embeddings.runtime backend probe depends on it — an upstream "
        "sync likely renamed or removed it. Update the proxy in "
        "src/codegraphcontext_ext/embeddings/runtime.py and this tripwire."
    )


def test_runtime_proxies_cover_every_required_upstream_helper():
    """Every upstream helper we depend on has a matching public proxy in runtime.py."""

    from codegraphcontext_ext.embeddings import runtime

    expected_proxy_names = {
        "_is_kuzudb_available": "is_kuzudb_available",
        "_is_falkordb_available": "is_falkordb_available",
        "_is_falkordb_remote_configured": "is_falkordb_remote_configured",
        "_is_neo4j_configured": "is_neo4j_configured",
    }

    for upstream_name, proxy_name in expected_proxy_names.items():
        assert upstream_name in REQUIRED_UPSTREAM_HELPERS
        assert callable(getattr(runtime, proxy_name, None)), (
            f"runtime.{proxy_name} should proxy codegraphcontext.core.{upstream_name}"
        )
