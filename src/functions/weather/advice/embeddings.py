"""Text embedding, behind a protocol with two implementations.

`AzureOpenAIEmbedder` is what production uses. `HashingEmbedder` is a
deterministic, dependency-free stand-in so that ingestion, retrieval tests and
CI can run with no Azure account and no cost — the same reason the retrieval
layer has a local implementation.

The hashing embedder is **not** a semantic model. It is good enough to exercise
the vector path end to end and to make hybrid fusion observable in tests; it is
not good enough to judge retrieval quality on. The eval harness reports which
embedder produced a run so the two are never compared as if equivalent.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from typing import Any, Protocol

logger = logging.getLogger(__name__)

DEFAULT_DIMENSIONS = 256
TOKEN_RE = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    name: str
    dimensions: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())


def cosine(a: tuple[float, ...] | list[float], b: tuple[float, ...] | list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


class HashingEmbedder:
    """A hashed bag-of-words projection: deterministic, offline, free.

    Every token is hashed into a bucket and weighted by a sub-linear term
    frequency, then the vector is L2-normalised so cosine similarity behaves.
    """

    name = "hashing-v1"

    def __init__(self, dimensions: int = DEFAULT_DIMENSIONS) -> None:
        self.dimensions = dimensions

    def _bucket(self, token: str) -> int:
        return int(hashlib.sha256(token.encode("utf-8")).hexdigest()[:8], 16) % self.dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            counts: dict[int, float] = {}
            for token in tokenize(text):
                counts[self._bucket(token)] = counts.get(self._bucket(token), 0.0) + 1.0
            vector = [0.0] * self.dimensions
            for bucket, count in counts.items():
                vector[bucket] = 1.0 + math.log(count)
            norm = math.sqrt(sum(v * v for v in vector))
            vectors.append([v / norm for v in vector] if norm else vector)
        return vectors


class AzureOpenAIEmbedder:
    """Azure OpenAI embeddings, authenticated by managed identity.

    No key is read from configuration or the environment: the same
    user-assigned identity the rest of the app uses gets a token for Cognitive
    Services. Nothing about this class can leak a credential into a log.
    """

    def __init__(
        self,
        endpoint: str,
        deployment: str,
        api_version: str = "2024-10-21",
        dimensions: int = 1536,
        credential: Any | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.name = f"azure-openai:{deployment}"
        self.dimensions = dimensions
        self._endpoint = endpoint.rstrip("/")
        self._deployment = deployment
        self._api_version = api_version
        self._timeout = timeout_seconds
        self._credential = credential
        self._client: Any = None

    def _ensure_client(self) -> Any:
        if self._client is None:
            from azure.identity import get_bearer_token_provider
            from openai import AzureOpenAI

            from .. import clients as azure_clients

            credential = self._credential or azure_clients.get_credential()
            self._client = AzureOpenAI(
                azure_endpoint=self._endpoint,
                api_version=self._api_version,
                azure_ad_token_provider=get_bearer_token_provider(
                    credential, "https://cognitiveservices.azure.com/.default"
                ),
                timeout=self._timeout,
                max_retries=1,  # one retry, then fall back — never a retry storm
            )
        return self._client

    def embed(self, texts: list[str]) -> list[list[float]]:
        response = self._ensure_client().embeddings.create(
            model=self._deployment, input=texts
        )
        return [item.embedding for item in response.data]


def get_embedder(settings: Any | None = None) -> Embedder:
    """The configured embedder, or the offline one when Azure OpenAI is unset."""
    rag = getattr(settings, "rag", None) if settings else None
    if rag and rag.openai_endpoint and rag.embedding_deployment:
        return AzureOpenAIEmbedder(
            endpoint=rag.openai_endpoint,
            deployment=rag.embedding_deployment,
            dimensions=rag.embedding_dimensions,
            timeout_seconds=rag.request_timeout_seconds,
        )
    logger.info("Azure OpenAI is not configured; using the offline hashing embedder.")
    return HashingEmbedder()
