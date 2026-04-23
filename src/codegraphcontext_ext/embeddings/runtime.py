"""Runtime helpers for the first cgraph embedding command slice."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.util import find_spec
import os
from typing import Optional

from ._upstream import (
    is_falkordb_available,
    is_falkordb_remote_configured,
    is_kuzudb_available,
    is_neo4j_configured,
)

SUPPORTED_BACKENDS = frozenset({"kuzudb", "falkordb"})
DEFAULT_PROVIDER = "local"

_PROVIDER_DEFAULTS = {
    "local": {"model": "jinaai/jina-embeddings-v2-base-code", "dimensions": 768},
    "voyage": {"model": "voyage-code-3", "dimensions": 1024},
    "openai": {"model": "text-embedding-3-large", "dimensions": 3072},
}


@dataclass(frozen=True)
class EmbeddingConfig:
    """Resolved provider settings for one embed invocation."""

    provider: str
    model: str
    dimensions: int


def available_providers() -> tuple[str, ...]:
    """Return the provider ids planned in the cgraph spec."""

    return tuple(_PROVIDER_DEFAULTS.keys())


def resolve_embedding_config(
    *,
    provider: Optional[str],
    model: Optional[str],
    dimensions: Optional[int],
) -> EmbeddingConfig:
    """Resolve provider settings, applying Phase 1 defaults."""

    resolved_provider = (provider or DEFAULT_PROVIDER).strip().lower()
    if resolved_provider not in _PROVIDER_DEFAULTS:
        choices = ", ".join(available_providers())
        raise ValueError(f"Unsupported embedding provider '{resolved_provider}'. Choose from: {choices}.")

    defaults = _PROVIDER_DEFAULTS[resolved_provider]
    resolved_model = model or str(defaults["model"])
    resolved_dimensions = dimensions or int(defaults["dimensions"])

    return EmbeddingConfig(
        provider=resolved_provider,
        model=resolved_model,
        dimensions=resolved_dimensions,
    )


def resolve_requested_backend() -> str:
    """Resolve the backend choice without triggering upstream fallback side effects.

    Checks (in order): CGC_RUNTIME_DB_TYPE env → DEFAULT_DATABASE env →
    DEFAULT_DATABASE from ~/.codegraphcontext/.env config file → probe
    installed backends.
    """
    runtime_db = os.environ.get("CGC_RUNTIME_DB_TYPE")
    explicit_db = runtime_db or os.environ.get("DEFAULT_DATABASE")
    if not explicit_db:
        try:
            from codegraphcontext.cli.config_manager import get_config_value
            explicit_db = get_config_value("DEFAULT_DATABASE")
        except Exception:
            pass
    if explicit_db:
        return explicit_db.lower()

    if is_falkordb_remote_configured():
        return "falkordb-remote"
    if is_falkordb_available():
        return "falkordb"
    if is_kuzudb_available():
        return "kuzudb"
    if is_neo4j_configured():
        return "neo4j"
    return "unavailable"


def active_local_backend() -> str:
    """Return the active embedded backend, defaulting to ``kuzudb``.

    Thin wrapper over ``resolve_requested_backend`` that coerces unknown
    or remote values down to the nearest local backend. Canonical helper
    for modules that branch on ``kuzudb`` vs. ``falkordb`` (schema,
    ANN, io/kuzu). Kept here rather than duplicated per module.
    """
    backend = (resolve_requested_backend() or "").lower()
    return backend if backend in SUPPORTED_BACKENDS else "kuzudb"


def probe_backend_support() -> dict[str, object]:
    """Report whether the current backend is usable for cgraph embeddings.

    Admits ``kuzudb`` or ``falkordb``. Per-backend write/read details
    live in ``embeddings/schema.py`` and ``hybrid/ann.py``.
    """

    backend = resolve_requested_backend()
    if backend not in SUPPORTED_BACKENDS:
        return {
            "ok": False,
            "kind": "unsupported_backend",
            "backend": backend,
            "detail": (
                f"cgraph embeddings support kuzudb or falkordb; found {backend}. "
                "Set CGC backend to kuzudb or falkordb and re-index."
            ),
        }

    if backend == "kuzudb" and not is_kuzudb_available():
        return {
            "ok": False,
            "kind": "missing_backend_dependency",
            "backend": backend,
            "detail": "KuzuDB is not installed. Run `pip install kuzu` before using cgraph embeddings.",
        }

    if backend == "falkordb" and not is_falkordb_available():
        return {
            "ok": False,
            "kind": "missing_backend_dependency",
            "backend": backend,
            "detail": "FalkorDB Lite is not installed. Run `pip install falkordblite` before using cgraph embeddings.",
        }

    return {
        "ok": True,
        "backend": backend,
    }


def build_model_check_payload(config: EmbeddingConfig, *, backend: str) -> dict[str, object]:
    """Build a non-mutating readiness payload for the embed command."""

    if config.provider == "local":
        cache_home = os.environ.get("SENTENCE_TRANSFORMERS_HOME") or os.environ.get("HF_HOME")
        if not has_local_embedding_runtime():
            return {
                "ok": False,
                "kind": "missing_dependency",
                "backend": backend,
                "provider": config.provider,
                "model": config.model,
                "dimensions": config.dimensions,
                "cache_home": cache_home,
                "detail": (
                    "Install `sentence-transformers` to use the local embedding provider. "
                    "This Phase 1 slice checks runtime availability but does not download weights."
                ),
            }

        return {
            "ok": True,
            "kind": "ready",
            "backend": backend,
            "provider": config.provider,
            "model": config.model,
            "dimensions": config.dimensions,
            "cache_home": cache_home,
            "detail": (
                "Local embedding runtime is installed. Cache/weight verification will land in a later "
                "Phase 1 pass."
            ),
        }

    if config.provider == "voyage":
        if not has_voyage_api_key():
            return {
                "ok": False,
                "kind": "missing_api_key",
                "backend": backend,
                "provider": config.provider,
                "model": config.model,
                "dimensions": config.dimensions,
                "detail": "Set VOYAGE_API_KEY to use the Voyage embedding provider.",
            }

        return {
            "ok": True,
            "kind": "ready",
            "backend": backend,
            "provider": config.provider,
            "model": config.model,
            "dimensions": config.dimensions,
            "detail": "Voyage API credentials are present.",
        }

    if not has_openai_api_key():
        return {
            "ok": False,
            "kind": "missing_api_key",
            "backend": backend,
            "provider": config.provider,
            "model": config.model,
            "dimensions": config.dimensions,
            "detail": "Set OPENAI_API_KEY to use the OpenAI embedding provider.",
        }

    return {
        "ok": True,
        "kind": "ready",
        "backend": backend,
        "provider": config.provider,
        "model": config.model,
        "dimensions": config.dimensions,
        "detail": "OpenAI API credentials are present.",
    }


def has_local_embedding_runtime() -> bool:
    """Return whether the local sentence-transformers runtime is installed."""

    return find_spec("sentence_transformers") is not None


def has_voyage_api_key() -> bool:
    """Return whether the Voyage API credential is available."""

    return bool(os.environ.get("VOYAGE_API_KEY"))


def has_openai_api_key() -> bool:
    """Return whether the OpenAI API credential is available."""

    return bool(os.environ.get("OPENAI_API_KEY"))


