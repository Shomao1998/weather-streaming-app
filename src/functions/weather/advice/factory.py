"""Choosing which content provider the API runs with.

One place decides, so the handler never has to know whether retrieval is
configured. The decision is made once per process and cached: building a
retriever loads and embeds the index, which is far too expensive to repeat on
every request.

Every branch that cannot produce a working RAG provider returns the phase-one
template provider instead of raising. An unconfigured or broken knowledge layer
must degrade the wording of a card, never the availability of one.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from ..config import Settings, get_settings
from .providers import AdviceContentProvider, TemplateAdviceProvider

logger = logging.getLogger(__name__)

_lock = threading.RLock()
_provider: AdviceContentProvider | None = None
_provider_signature: tuple[Any, ...] | None = None


def _resolve_index_path(raw: str) -> Path:
    path = Path(raw)
    if path.is_absolute():
        return path
    # Deployed, the index sits inside the package (src/functions/knowledge/,
    # staged there by CI). Locally it sits at the repository root, where it is
    # authored and evaluated. Both are resolved explicitly rather than trusting
    # the cwd, which differs between the host, pytest and the eval scripts.
    #
    #   parents[0] advice  [1] weather  [2] src/functions  [3] src  [4] repo root
    here = Path(__file__).resolve()
    for base in (here.parents[2], here.parents[4], Path.cwd()):
        candidate = base / path
        if candidate.exists():
            return candidate
    return path


def build_retriever(settings: Settings) -> Any | None:
    """The production retriever when Azure AI Search is configured, otherwise
    the local one. Returns None when neither can be built."""
    from .embeddings import get_embedder
    from .knowledge import KnowledgeIndex
    from .retrieval import AzureSearchRetriever, LocalIndexRetriever

    rag = settings.rag
    embedder = get_embedder(settings)

    if rag.search_endpoint:
        return AzureSearchRetriever(
            endpoint=rag.search_endpoint,
            index_name=rag.search_index_name,
            embedder=embedder,
            timeout_seconds=rag.request_timeout_seconds,
            use_semantic_ranker=rag.use_semantic_ranker,
        )

    path = _resolve_index_path(rag.index_path)
    if not path.exists():
        logger.warning("Knowledge index %s not found; advice stays on templates.", path)
        return None
    try:
        index = KnowledgeIndex.load(path)
    except Exception:
        logger.exception("Knowledge index %s could not be loaded.", path)
        return None

    if index.embedding_model != embedder.name:
        # Comparing a query embedded by one model against vectors produced by
        # another gives meaningless similarities. Keyword ranking still works,
        # so this is a warning rather than a refusal — but it must be visible.
        logger.warning(
            "Index was built with embedder %r but %r is configured; "
            "vector ranking will be unreliable until the index is rebuilt.",
            index.embedding_model,
            embedder.name,
        )
    return LocalIndexRetriever(index, embedder)


def build_provider(settings: Settings | None = None) -> AdviceContentProvider:
    """Build a provider without consulting or populating the cache."""
    settings = settings or get_settings()
    fallback = TemplateAdviceProvider()

    if not settings.rag.enabled:
        return fallback

    from .llm import get_chat_client
    from .rag import RagAdviceProvider

    chat = get_chat_client(settings)
    if chat is None:
        logger.info("RAG is enabled but no chat deployment is configured; using templates.")
        return fallback

    retriever = build_retriever(settings)
    if retriever is None:
        return fallback

    logger.info(
        "Advice provider: rag-v1 (retriever=%s, index_version=%s, model=%s)",
        retriever.name,
        retriever.index_version,
        chat.model,
    )
    return RagAdviceProvider(
        retriever=retriever, chat_client=chat, settings=settings, fallback=fallback
    )


def get_provider(settings: Settings | None = None) -> AdviceContentProvider:
    """The process-wide provider, built once."""
    global _provider, _provider_signature
    settings = settings or get_settings()
    rag = settings.rag
    signature = (
        rag.enabled,
        rag.index_path,
        rag.search_endpoint,
        rag.search_index_name,
        rag.openai_endpoint,
        rag.chat_deployment,
        rag.embedding_deployment,
    )
    with _lock:
        if _provider is None or _provider_signature != signature:
            _provider = build_provider(settings)
            _provider_signature = signature
        return _provider


def reset_provider() -> None:
    """Drop the cached provider. Used by tests and after a config change."""
    global _provider, _provider_signature
    with _lock:
        _provider = None
        _provider_signature = None
