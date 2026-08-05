"""Retrieval, behind one protocol with two implementations.

`AzureSearchRetriever` is production. `LocalIndexRetriever` runs the same
hybrid strategy over a JSON index file with no network and no cost, which is
what makes the retrieval layer testable in CI and what keeps the feature
buildable while the subscription is unavailable.

No Azure AI Search SDK call appears outside this module.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .embeddings import Embedder, cosine, tokenize
from .knowledge import KnowledgeChunk, KnowledgeIndex

logger = logging.getLogger(__name__)

# Reciprocal-rank-fusion constant. 60 is the value Azure AI Search uses for
# hybrid queries; keeping it identical means the local retriever ranks the way
# the production one does, so a CI result is meaningful.
RRF_K = 60


class RetrievalError(RuntimeError):
    """Retrieval failed. The caller falls back rather than surfacing this."""


@dataclass(frozen=True)
class RetrievalQuery:
    """Everything the retriever is allowed to consider.

    Filters are explicit rather than folded into the query text: a UV trigger
    must not be able to drift into flood documents because the prose happened
    to mention water.
    """

    text: str
    hazard_types: tuple[str, ...]
    jurisdiction: str = ""
    locale: str = ""
    top_k: int = 4
    min_score: float = 0.0

    def cache_key(self, index_version: str) -> str:
        raw = json.dumps(
            {
                "text": self.text,
                "hazard_types": sorted(self.hazard_types),
                "jurisdiction": self.jurisdiction,
                "locale": self.locale,
                "top_k": self.top_k,
                "index_version": index_version,
            },
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def filter_description(self) -> dict[str, Any]:
        """What was filtered on, for the structured log."""
        return {
            "hazard_types": list(self.hazard_types),
            "jurisdiction": self.jurisdiction or "*",
            "locale": self.locale or "*",
            "enabled": True,
        }


@dataclass(frozen=True)
class RetrievedChunk:
    chunk: KnowledgeChunk
    score: float
    keyword_rank: int | None = None
    vector_rank: int | None = None

    @property
    def chunk_id(self) -> str:
        return self.chunk.chunk_id


@dataclass
class RetrievalResult:
    chunks: list[RetrievedChunk] = field(default_factory=list)
    index_version: str = ""
    latency_ms: float = 0.0
    cache_hit: bool = False
    retriever: str = ""

    @property
    def chunk_ids(self) -> list[str]:
        return [c.chunk_id for c in self.chunks]


class AdviceRetriever(Protocol):
    name: str
    index_version: str

    def retrieve(self, query: RetrievalQuery) -> list[RetrievedChunk]: ...


def _matches_filters(chunk: KnowledgeChunk, query: RetrievalQuery, now: datetime) -> bool:
    if not chunk.is_effective(now):
        return False
    if query.hazard_types and not set(chunk.hazard_types) & set(query.hazard_types):
        return False
    if query.jurisdiction and chunk.jurisdiction and chunk.jurisdiction != query.jurisdiction:
        return False
    return not (query.locale and chunk.locale and chunk.locale != query.locale)


class LocalIndexRetriever:
    """Hybrid retrieval over a JSON index: BM25-style keyword plus cosine
    vector similarity, fused with reciprocal rank fusion.

    Deliberately mirrors what Azure AI Search does for a hybrid query, so a
    ranking observed locally is not an artefact of a different algorithm.
    """

    name = "local-hybrid"

    def __init__(self, index: KnowledgeIndex, embedder: Embedder) -> None:
        self._index = index
        self._embedder = embedder
        self.index_version = index.index_version
        self._df: dict[str, int] = defaultdict(int)
        for chunk in index.chunks:
            for token in set(tokenize(f"{chunk.heading} {chunk.content}")):
                self._df[token] += 1
        self._total = max(len(index.chunks), 1)

    def _keyword_scores(
        self, query_tokens: list[str], candidates: list[KnowledgeChunk]
    ) -> dict[str, float]:
        scores: dict[str, float] = {}
        for chunk in candidates:
            tokens = tokenize(f"{chunk.heading} {chunk.content}")
            if not tokens:
                continue
            counts: dict[str, int] = defaultdict(int)
            for token in tokens:
                counts[token] += 1
            score = 0.0
            for token in set(query_tokens):
                if token not in counts:
                    continue
                # Sub-linear term frequency times inverse document frequency:
                # a term that appears in every chunk carries no signal.
                tf = 1.0 + math.log(counts[token])
                idf = math.log((self._total + 1) / (self._df.get(token, 0) + 1)) + 1.0
                score += tf * idf
            if score > 0:
                scores[chunk.chunk_id] = score / math.sqrt(len(tokens))
        return scores

    def retrieve(self, query: RetrievalQuery) -> list[RetrievedChunk]:
        now = datetime.now(UTC)
        candidates = [c for c in self._index.chunks if _matches_filters(c, query, now)]
        if not candidates:
            return []

        keyword = self._keyword_scores(tokenize(query.text), candidates)
        keyword_order = sorted(keyword.items(), key=lambda kv: -kv[1])
        keyword_rank = {cid: i + 1 for i, (cid, _) in enumerate(keyword_order)}

        vector_rank: dict[str, int] = {}
        try:
            embedded = self._embedder.embed([query.text])[0]
            similarity = {
                c.chunk_id: cosine(embedded, c.content_vector)
                for c in candidates
                if c.content_vector
            }
            vector_order = sorted(similarity.items(), key=lambda kv: -kv[1])
            vector_rank = {cid: i + 1 for i, (cid, _) in enumerate(vector_order)}
        except Exception:
            # A vector failure degrades hybrid to keyword-only rather than
            # failing the request; keyword alone is still useful.
            logger.warning("Vector scoring unavailable; using keyword ranking only.")

        fused: dict[str, float] = defaultdict(float)
        for cid, rank in keyword_rank.items():
            fused[cid] += 1.0 / (RRF_K + rank)
        for cid, rank in vector_rank.items():
            fused[cid] += 1.0 / (RRF_K + rank)

        by_id = {c.chunk_id: c for c in candidates}
        ranked = sorted(fused.items(), key=lambda kv: -kv[1])
        results = [
            RetrievedChunk(
                chunk=by_id[cid],
                score=score,
                keyword_rank=keyword_rank.get(cid),
                vector_rank=vector_rank.get(cid),
            )
            for cid, score in ranked
            if score >= query.min_score
        ]
        return results[: query.top_k]


class AzureSearchRetriever:
    """Hybrid retrieval against Azure AI Search, by managed identity.

    Filters are an OData expression rather than post-processing, so a disabled
    or out-of-jurisdiction document is excluded by the service and can never
    be paid for, ranked, or accidentally returned by a code path that forgot
    to filter.
    """

    name = "azure-ai-search"

    def __init__(
        self,
        endpoint: str,
        index_name: str,
        embedder: Embedder,
        index_version: str = "",
        credential: Any | None = None,
        timeout_seconds: float = 5.0,
        use_semantic_ranker: bool = False,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._index_name = index_name
        self._embedder = embedder
        self.index_version = index_version
        self._credential = credential
        self._timeout = timeout_seconds
        self._use_semantic = use_semantic_ranker
        self._client: Any = None
        self._lock = threading.RLock()

    def _ensure_client(self) -> Any:
        with self._lock:
            if self._client is None:
                from azure.search.documents import SearchClient

                from .. import clients as azure_clients

                self._client = SearchClient(
                    endpoint=self._endpoint,
                    index_name=self._index_name,
                    credential=self._credential or azure_clients.get_credential(),
                )
            return self._client

    @staticmethod
    def build_filter(query: RetrievalQuery) -> str:
        """OData filter. `enabled` is always asserted, never optional."""
        clauses = ["enabled eq true"]
        if query.hazard_types:
            hazards = " or ".join(f"h eq '{h}'" for h in sorted(query.hazard_types))
            clauses.append(f"hazard_types/any(h: {hazards})")
        if query.jurisdiction:
            clauses.append(f"jurisdiction eq '{query.jurisdiction}'")
        if query.locale:
            clauses.append(f"locale eq '{query.locale}'")
        return " and ".join(clauses)

    def retrieve(self, query: RetrievalQuery) -> list[RetrievedChunk]:
        try:
            from azure.search.documents.models import VectorizedQuery

            vector = self._embedder.embed([query.text])[0]
            kwargs: dict[str, Any] = {
                "search_text": query.text,
                "vector_queries": [
                    VectorizedQuery(
                        vector=vector, k_nearest_neighbors=query.top_k * 3,
                        fields="content_vector",
                    )
                ],
                "filter": self.build_filter(query),
                "top": query.top_k,
                "select": [
                    "chunk_id", "source_document_id", "title", "content", "hazard_types",
                    "severity", "authority", "jurisdiction", "locale", "effective_from",
                    "last_verified_at", "source_url", "version", "enabled", "heading",
                ],
            }
            if self._use_semantic:
                kwargs["query_type"] = "semantic"
                kwargs["semantic_configuration_name"] = "default"

            results = self._ensure_client().search(**kwargs)
            found: list[RetrievedChunk] = []
            for rank, doc in enumerate(results, start=1):
                payload = dict(doc)
                score = float(payload.get("@search.score", 0.0))
                payload.pop("@search.score", None)
                payload.pop("@search.reranker_score", None)
                found.append(
                    RetrievedChunk(
                        chunk=KnowledgeChunk.from_dict(payload), score=score, keyword_rank=rank
                    )
                )
            return found
        except Exception as exc:
            raise RetrievalError(str(exc)) from exc


def build_local_retriever(index_path: str | Path, embedder: Embedder) -> LocalIndexRetriever:
    return LocalIndexRetriever(KnowledgeIndex.load(index_path), embedder)
