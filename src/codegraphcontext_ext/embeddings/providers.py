"""Embedding provider abstraction for cgraph.

Spec §6.1: default is local jina-embeddings-v2-base-code (768-dim, 8K ctx).
Swappable to voyage-code-3 or text-embedding-3-large via config.
"""

from __future__ import annotations

import sys
from typing import Protocol

from .runtime import EmbeddingConfig, available_providers


class EmbeddingProvider(Protocol):
    """Interface for embedding providers."""

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per input text."""
        ...

    @property
    def dimensions(self) -> int: ...


class LocalProvider:
    """sentence-transformers provider using a HuggingFace model (default: Jina v2 code)."""

    def __init__(self, config: EmbeddingConfig) -> None:
        self._config = config
        self._model = None

    @property
    def dimensions(self) -> int:
        return self._config.dimensions

    def _load_model(self):
        if self._model is not None:
            return
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise RuntimeError(
                "sentence-transformers is required for local embeddings. "
                "Install with: pip install sentence-transformers"
            )
        self._model = SentenceTransformer(
            self._config.model, trust_remote_code=True
        )
        print(
            f"Loaded embedding model {self._config.model}",
            file=sys.stderr,
        )

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self._load_model()
        embeddings = self._model.encode(texts, show_progress_bar=False)
        return [vec.tolist() for vec in embeddings]


class VoyageProvider:
    """Voyage AI API provider."""

    def __init__(self, config: EmbeddingConfig) -> None:
        self._config = config

    @property
    def dimensions(self) -> int:
        return self._config.dimensions

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        import os

        try:
            import voyageai
        except ImportError:
            raise RuntimeError(
                "voyageai is required for the Voyage provider. "
                "Install with: pip install voyageai"
            )
        client = voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"])
        result = client.embed(texts, model=self._config.model)
        return result.embeddings


class OpenAIProvider:
    """OpenAI API provider."""

    def __init__(self, config: EmbeddingConfig) -> None:
        self._config = config

    @property
    def dimensions(self) -> int:
        return self._config.dimensions

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        import os

        try:
            import openai
        except ImportError:
            raise RuntimeError(
                "openai is required for the OpenAI provider. "
                "Install with: pip install openai"
            )
        client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        response = client.embeddings.create(
            input=texts,
            model=self._config.model,
            dimensions=self._config.dimensions,
        )
        return [item.embedding for item in response.data]


_PROVIDER_CLASSES: dict[str, type] = {
    "local": LocalProvider,
    "voyage": VoyageProvider,
    "openai": OpenAIProvider,
}


def create_provider(config: EmbeddingConfig) -> EmbeddingProvider:
    """Factory: instantiate the right provider from resolved config."""
    cls = _PROVIDER_CLASSES.get(config.provider)
    if cls is None:
        choices = ", ".join(available_providers())
        raise ValueError(
            f"No provider class for '{config.provider}'. Choose from: {choices}."
        )
    return cls(config)
