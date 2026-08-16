"""Retrieval: filtering, ranking, and the production query it stands in for.

The local retriever is exercised against the real corpus, so these are
integration tests of the retrieval layer, not mocks of it. `AzureSearchRetriever`
cannot be reached without a subscription, so what is asserted there is the part
that is pure and therefore checkable: the OData filter and the index schema it
depends on. Those two are exactly where a silent mistake would let a disabled
or out-of-scope document be cited.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from weather.advice.embeddings import HashingEmbedder  # noqa: E402
from weather.advice.knowledge import KnowledgeIndex  # noqa: E402
from weather.advice.retrieval import (  # noqa: E402
    RRF_K,
    AzureSearchRetriever,
    LocalIndexRetriever,
    RetrievalError,
    RetrievalQuery,
)


def query(text: str, hazards: tuple[str, ...], **kwargs) -> RetrievalQuery:
    return RetrievalQuery(text=text, hazard_types=hazards, **kwargs)


# -- metadata filtering -----------------------------------------------------


@pytest.mark.parametrize(
    ("hazard", "expected_document"),
    [
        ("heat", "nws-heat-during"),
        ("uv", "epa-uv-index-scale"),
        ("wind", "nws-wind-during"),
        ("rain", "nws-flood-during"),
        ("air_quality", "airnow-aqi-basics"),
    ],
)
def test_a_hazard_filter_confines_retrieval_to_that_corpus(
    local_retriever, hazard, expected_document
):
    results = local_retriever.retrieve(query("safety guidance", (hazard,), top_k=10))
    assert results
    assert {r.chunk.source_document_id for r in results} == {expected_document}


def test_a_uv_query_cannot_reach_flood_guidance(local_retriever):
    """The drift this filter exists to prevent: 'water' appears in both."""
    results = local_retriever.retrieve(
        query("drink water and protect skin from the sun", ("uv",), top_k=10)
    )
    assert results
    assert all("flood" not in r.chunk.source_document_id for r in results)


def test_a_disabled_chunk_is_never_returned(knowledge_index):
    disabled = replace(knowledge_index.chunks[0], enabled=False)
    index = KnowledgeIndex(
        index_version=knowledge_index.index_version,
        chunks=(disabled,) + knowledge_index.chunks[1:],
        embedding_model=knowledge_index.embedding_model,
    )
    retriever = LocalIndexRetriever(index, HashingEmbedder())
    results = retriever.retrieve(query("heat", ("heat",), top_k=20))
    assert disabled.chunk_id not in {r.chunk_id for r in results}


def test_an_expired_chunk_is_never_returned(knowledge_index):
    yesterday = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    expired = replace(knowledge_index.chunks[0], expires_at=yesterday)
    index = KnowledgeIndex(
        index_version="test",
        chunks=(expired,) + knowledge_index.chunks[1:],
        embedding_model=knowledge_index.embedding_model,
    )
    retriever = LocalIndexRetriever(index, HashingEmbedder())
    results = retriever.retrieve(query("heat", ("heat",), top_k=20))
    assert expired.chunk_id not in {r.chunk_id for r in results}


def test_a_chunk_not_yet_effective_is_never_returned(knowledge_index):
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    future = replace(knowledge_index.chunks[0], effective_from=tomorrow)
    index = KnowledgeIndex(
        index_version="test",
        chunks=(future,) + knowledge_index.chunks[1:],
        embedding_model=knowledge_index.embedding_model,
    )
    retriever = LocalIndexRetriever(index, HashingEmbedder())
    assert future.chunk_id not in {
        r.chunk_id for r in retriever.retrieve(query("heat", ("heat",), top_k=20))
    }


def test_a_mismatched_jurisdiction_excludes_everything(local_retriever):
    assert local_retriever.retrieve(query("heat", ("heat",), jurisdiction="JP")) == []


def test_an_unmatched_hazard_returns_nothing_rather_than_anything(local_retriever):
    assert local_retriever.retrieve(query("earthquake", ("volcano",))) == []


# -- ranking ----------------------------------------------------------------


def test_top_k_is_respected(local_retriever):
    assert len(local_retriever.retrieve(query("heat safety", ("heat",), top_k=2))) == 2


def test_hybrid_fusion_uses_both_signals(local_retriever):
    results = local_retriever.retrieve(query("hydration drink water", ("heat",), top_k=4))
    assert results
    top = results[0]
    assert top.keyword_rank is not None and top.vector_rank is not None
    # RRF, not a raw score: the fused value is bounded by two reciprocal terms.
    assert 0 < top.score <= 2 / (RRF_K + 1)


def test_results_are_ordered_by_fused_score(local_retriever):
    scores = [r.score for r in local_retriever.retrieve(query("wind", ("wind",), top_k=5))]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.parametrize(
    ("text", "hazard", "expected_heading_fragment"),
    [
        ("how much water should I drink in the heat", "heat", "Hydration"),
        ("is it safe to drive in high wind", "wind", "Driving"),
        ("can I walk through floodwater", "rain", "On foot"),
        ("uv index is extremely high what do I do", "uv", "Very High"),
    ],
)
def test_a_specific_question_retrieves_the_matching_passage(
    local_retriever, text, hazard, expected_heading_fragment
):
    """Ranking quality, on the real corpus, not just plumbing."""
    results = local_retriever.retrieve(query(text, (hazard,), top_k=3))
    assert any(expected_heading_fragment in r.chunk.heading for r in results), [
        r.chunk.heading for r in results
    ]


def test_keyword_ranking_survives_an_embedder_failure(knowledge_index):
    class BrokenEmbedder:
        name = "broken"

        def embed(self, texts):
            raise RuntimeError("embedding service unavailable")

    retriever = LocalIndexRetriever(knowledge_index, BrokenEmbedder())
    results = retriever.retrieve(query("hydration drink water", ("heat",), top_k=3))
    assert results, "keyword-only ranking should still return passages"
    assert all(r.vector_rank is None for r in results)


# -- the Azure implementation -----------------------------------------------


def test_the_odata_filter_always_asserts_enabled():
    built = AzureSearchRetriever.build_filter(query("x", ("heat",)))
    assert built.startswith("enabled eq true")


def test_the_odata_filter_covers_every_metadata_dimension():
    built = AzureSearchRetriever.build_filter(
        query("x", ("heat", "uv"), jurisdiction="US", locale="en")
    )
    assert "hazard_types/any(h: h eq 'heat' or h eq 'uv')" in built
    assert "jurisdiction eq 'US'" in built
    assert "locale eq 'en'" in built


def test_an_empty_hazard_filter_still_excludes_disabled_documents():
    assert AzureSearchRetriever.build_filter(query("x", ())) == "enabled eq true"


def test_a_search_failure_is_raised_as_retrieval_error():
    class Failing(AzureSearchRetriever):
        def _ensure_client(self):
            raise RuntimeError("service unavailable")

    retriever = Failing("https://example.search.windows.net", "i", HashingEmbedder())
    with pytest.raises(RetrievalError):
        retriever.retrieve(query("heat", ("heat",)))


def test_the_index_schema_supports_every_filter_the_retriever_builds():
    """A schema that cannot serve the filter fails at query time, not deploy
    time — so the two are asserted against each other here."""
    from build_search_index import VECTOR_FIELD, index_schema

    schema = index_schema("weather-advice", 256)
    fields = {f["name"]: f for f in schema["fields"]}

    for name in ("enabled", "hazard_types", "jurisdiction", "locale"):
        assert fields[name].get("filterable") is True, name
    assert fields["enabled"]["type"] == "Edm.Boolean"
    assert fields["chunk_id"].get("key") is True
    assert fields[VECTOR_FIELD]["dimensions"] == 256
    assert fields[VECTOR_FIELD]["vectorSearchProfile"] in {
        p["name"] for p in schema["vectorSearch"]["profiles"]
    }
    assert schema["vectorSearch"]["algorithms"][0]["hnswParameters"]["metric"] == "cosine"


def test_every_indexed_document_carries_the_fields_the_filter_needs(knowledge_index):
    from build_search_index import to_documents

    for document in to_documents(knowledge_index):
        assert "enabled" in document
        assert isinstance(document["hazard_types"], list)
        assert document["content_vector"]
