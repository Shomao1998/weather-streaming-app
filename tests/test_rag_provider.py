"""`RagAdviceProvider`: fallback, caching, telemetry and the phase-one contract.

The recurring assertion in this file is that *every* failure produces a
deterministic card rather than no card or a bad one. A retrieval-grounded
feature is only acceptable here if the page is exactly as reliable with it
switched on as it was without it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from weather.advice import llm as llm_module
from weather.advice.cache import TtlCache, generation_key
from weather.advice.models import AdviceTrigger, WeatherContext
from weather.advice.providers import TemplateAdviceProvider
from weather.advice.rag import TRIGGER_HAZARDS, RagAdviceProvider, estimate_cost
from weather.advice.retrieval import RetrievalError
from weather.config import RagSettings, Settings


@pytest.fixture
def rag_settings() -> Settings:
    """Settings are constructed rather than loaded from the environment, the
    way the phase-one advice tests do it: what is under test here is the
    provider, not the config parser."""
    return Settings(rag=RagSettings(enabled=True))


@pytest.fixture
def hot_weather() -> WeatherContext:
    return WeatherContext(
        location="Tokyo",
        location_key="tokyo",
        observed_at_utc=datetime(2026, 8, 5, 6, 0, tzinfo=UTC),
        temp_c=36.4,
        feelslike_c=41.0,
        uv=9.0,
        wind_kph=12.0,
        precip_chance_next_hour=5,
        condition_text="Sunny",
    )


def scripted(payload: dict | None = None, count: int = 8) -> llm_module.ScriptedChatClient:
    """A client that returns one fixed, valid response every time."""
    if payload is None:
        payload = {
            "title": "高温注意",
            "message": "多补水，避开正午的户外活动。",
            "advice_codes": ["HYDRATE", "RESCHEDULE_STRENUOUS_ACTIVITY"],
            "supporting_chunk_ids": ["__FIRST__"],
        }
    return llm_module.ScriptedChatClient([json.dumps(payload, ensure_ascii=False)] * count)


def provider_with(retriever, chat, settings) -> RagAdviceProvider:
    return RagAdviceProvider(retriever=retriever, chat_client=chat, settings=settings)


def valid_response_for(retriever, trigger, weather, settings) -> llm_module.ScriptedChatClient:
    """Build a response that cites a chunk this query will actually retrieve."""
    probe = RagAdviceProvider(retriever=retriever, chat_client=scripted(), settings=settings)
    chunks = retriever.retrieve(probe.build_query(trigger, weather))
    assert chunks, "expected the corpus to answer this trigger"
    return scripted(
        {
            "title": "高温注意",
            "message": "多补水，避开正午的户外活动。",
            "advice_codes": ["HYDRATE"],
            "supporting_chunk_ids": [chunks[0].chunk_id],
        }
    )


# -- the happy path ---------------------------------------------------------


def test_a_grounded_card_carries_resolvable_sources(local_retriever, hot_weather, rag_settings):
    chat = valid_response_for(local_retriever, AdviceTrigger.EXTREME_HEAT, hot_weather, rag_settings)
    provider = provider_with(local_retriever, chat, rag_settings)

    content = provider.generate(AdviceTrigger.EXTREME_HEAT, hot_weather)

    assert content.generation_method == "rag-v1"
    assert content.sources
    for source in content.sources:
        assert source.chunk_id and source.authority and source.source_url.startswith("https://")
    assert content.advice_codes == ("HYDRATE",)
    assert provider.last_telemetry.validation == "ok"


def test_the_prompt_contains_only_the_facts_a_rule_used(
    local_retriever, hot_weather, rag_settings
):
    """Not the whole weather record and not the whole corpus."""
    chat = valid_response_for(local_retriever, AdviceTrigger.EXTREME_HEAT, hot_weather, rag_settings)
    provider_with(local_retriever, chat, rag_settings).generate(
        AdviceTrigger.EXTREME_HEAT, hot_weather
    )

    _system, user = chat.calls[0]
    facts = json.loads(user.split("WEATHER_FACTS\n")[1].split("\n\nTRIGGER")[0])
    guidance = json.loads(user.split("RETRIEVED_GUIDANCE\n")[1].split("\n\nOUTPUT_SCHEMA")[0])

    assert facts["temperature_c"] == 36.4
    assert len(guidance) <= rag_settings.rag.top_k
    # Every passage in the prompt is one the card is allowed to cite.
    assert all(item["chunk_id"] for item in guidance)


@pytest.mark.parametrize(
    ("trigger", "hazard"),
    [
        (AdviceTrigger.EXTREME_HEAT, "heat"),
        (AdviceTrigger.HIGH_UV, "uv"),
        (AdviceTrigger.HIGH_WIND, "wind"),
        (AdviceTrigger.RAIN_EXPECTED, "rain"),
    ],
)
def test_each_trigger_maps_to_its_own_hazard_filter(
    local_retriever, hot_weather, rag_settings, trigger, hazard
):
    provider = provider_with(local_retriever, scripted(), rag_settings)
    query = provider.build_query(trigger, hot_weather)
    assert query.hazard_types == TRIGGER_HAZARDS[trigger] == (hazard,)


def test_live_readings_steer_retrieval(local_retriever, hot_weather, rag_settings):
    provider = provider_with(local_retriever, scripted(), rag_settings)
    assert "uv index 9" in provider.build_query(AdviceTrigger.HIGH_UV, hot_weather).text


# -- fallback, on every failure path ----------------------------------------


def _assert_fell_back(content, provider, fragment: str):
    assert content.generation_method == TemplateAdviceProvider.name
    assert content.title and content.message
    assert not content.sources
    assert fragment in provider.last_telemetry.fallback_reason


def test_retrieval_failure_falls_back(hot_weather, rag_settings):
    class Failing:
        name, index_version = "failing", "v1"

        def retrieve(self, query):
            raise RetrievalError("search service unavailable")

    provider = provider_with(Failing(), scripted(), rag_settings)
    _assert_fell_back(
        provider.generate(AdviceTrigger.EXTREME_HEAT, hot_weather), provider, "retrieval failed"
    )


def test_an_unexpected_retriever_exception_falls_back(hot_weather, rag_settings):
    class Exploding:
        name, index_version = "exploding", "v1"

        def retrieve(self, query):
            raise ZeroDivisionError("a bug, not a service failure")

    provider = provider_with(Exploding(), scripted(), rag_settings)
    _assert_fell_back(
        provider.generate(AdviceTrigger.EXTREME_HEAT, hot_weather), provider, "retrieval error"
    )


def test_insufficient_knowledge_falls_back(hot_weather, rag_settings):
    class Empty:
        name, index_version = "empty", "v1"

        def retrieve(self, query):
            return []

    provider = provider_with(Empty(), scripted(), rag_settings)
    _assert_fell_back(
        provider.generate(AdviceTrigger.EXTREME_HEAT, hot_weather), provider, "usable chunks"
    )


def test_a_model_timeout_falls_back(local_retriever, hot_weather, rag_settings):
    class TimingOut:
        name, model = "slow", "gpt-test"

        def complete(self, system, user):
            raise llm_module.LlmTimeout("model timed out after 8000ms")

    provider = provider_with(local_retriever, TimingOut(), rag_settings)
    _assert_fell_back(
        provider.generate(AdviceTrigger.EXTREME_HEAT, hot_weather), provider, "model timeout"
    )


def test_a_model_error_falls_back(local_retriever, hot_weather, rag_settings):
    class Failing:
        name, model = "broken", "gpt-test"

        def complete(self, system, user):
            raise llm_module.LlmError("429 rate limited")

    provider = provider_with(local_retriever, Failing(), rag_settings)
    _assert_fell_back(
        provider.generate(AdviceTrigger.EXTREME_HEAT, hot_weather), provider, "model error"
    )


def test_invalid_structured_output_falls_back(local_retriever, hot_weather, rag_settings):
    provider = provider_with(
        local_retriever, llm_module.ScriptedChatClient(["sorry, I can't do that"]), rag_settings
    )
    _assert_fell_back(
        provider.generate(AdviceTrigger.EXTREME_HEAT, hot_weather), provider, "structured JSON"
    )


def test_a_fabricated_citation_falls_back(local_retriever, hot_weather, rag_settings):
    """The most important one: plausible copy with an unverifiable source is
    treated exactly like a crash."""
    provider = provider_with(
        local_retriever,
        scripted(
            {
                "title": "高温注意",
                "message": "多补水。",
                "advice_codes": ["HYDRATE"],
                "supporting_chunk_ids": ["nws-heat-during-042-fabricated0"],
            }
        ),
        rag_settings,
    )
    _assert_fell_back(
        provider.generate(AdviceTrigger.EXTREME_HEAT, hot_weather), provider, "validation failed"
    )


def test_an_abstention_falls_back(local_retriever, hot_weather, rag_settings):
    provider = provider_with(
        local_retriever, llm_module.ScriptedChatClient(['{"abstain": true}']), rag_settings
    )
    _assert_fell_back(
        provider.generate(AdviceTrigger.EXTREME_HEAT, hot_weather), provider, "abstained"
    )


def test_rag_disabled_falls_back(local_retriever, hot_weather):
    settings = Settings(rag=RagSettings(enabled=False))
    provider = provider_with(local_retriever, scripted(), settings)
    _assert_fell_back(
        provider.generate(AdviceTrigger.EXTREME_HEAT, hot_weather), provider, "rag disabled"
    )


def test_no_chat_client_falls_back(local_retriever, hot_weather, rag_settings):
    provider = provider_with(local_retriever, None, rag_settings)
    _assert_fell_back(
        provider.generate(AdviceTrigger.EXTREME_HEAT, hot_weather), provider, "no chat client"
    )


def test_a_severe_trigger_still_gets_a_card_when_everything_fails(
    hot_weather, rag_settings
):
    """The user's explicit requirement: high-severity advice must survive a
    total RAG outage."""
    class Dead:
        name, index_version = "dead", "v1"

        def retrieve(self, query):
            raise RetrievalError("everything is down")

    provider = provider_with(Dead(), None, rag_settings)
    for trigger in TRIGGER_HAZARDS:
        content = provider.generate(trigger, hot_weather)
        assert content.title and content.message
        assert content.generation_method == TemplateAdviceProvider.name


# -- caching ----------------------------------------------------------------


def test_the_same_snapshot_hits_the_generation_cache(
    local_retriever, hot_weather, rag_settings
):
    chat = valid_response_for(local_retriever, AdviceTrigger.EXTREME_HEAT, hot_weather, rag_settings)
    provider = provider_with(local_retriever, chat, rag_settings)

    first = provider.generate(AdviceTrigger.EXTREME_HEAT, hot_weather)
    second = provider.generate(AdviceTrigger.EXTREME_HEAT, hot_weather)

    assert len(chat.calls) == 1, "the model should be called once per snapshot"
    assert second.title == first.title
    assert provider.last_telemetry.generation_cache_hit


def test_a_new_observation_is_not_served_from_cache(
    local_retriever, hot_weather, rag_settings
):
    from dataclasses import replace

    chat = valid_response_for(local_retriever, AdviceTrigger.EXTREME_HEAT, hot_weather, rag_settings)
    provider = provider_with(local_retriever, chat, rag_settings)

    provider.generate(AdviceTrigger.EXTREME_HEAT, hot_weather)
    later = replace(hot_weather, observed_at_utc=datetime(2026, 8, 5, 7, 0, tzinfo=UTC))
    provider.generate(AdviceTrigger.EXTREME_HEAT, later)

    assert len(chat.calls) == 2


def test_a_knowledge_version_change_invalidates_the_cache():
    """A re-ingest must not keep serving answers grounded in the old corpus."""
    first = generation_key(
        weather_snapshot_id="s", trigger="t", chunk_ids=["a"],
        prompt_version="p", model="m", index_version="2026-08-05.1",
    )
    second = generation_key(
        weather_snapshot_id="s", trigger="t", chunk_ids=["a"],
        prompt_version="p", model="m", index_version="2026-09-01.1",
    )
    assert first != second


def test_a_prompt_change_invalidates_the_cache():
    base = {
        "weather_snapshot_id": "s", "trigger": "t", "chunk_ids": ["a"],
        "model": "m", "index_version": "v",
    }
    assert generation_key(prompt_version="p1", **base) != generation_key(
        prompt_version="p2", **base
    )


def test_a_different_question_is_a_different_cache_entry():
    base = {
        "weather_snapshot_id": "s", "trigger": "t", "chunk_ids": ["a"],
        "prompt_version": "p", "model": "m", "index_version": "v",
    }
    assert generation_key(question="需要打伞吗", **base) != generation_key(
        question="能跑步吗", **base
    )


def test_retrieval_is_cached_across_snapshots(local_retriever, hot_weather, rag_settings):
    """Retrieval depends on the query, not the observation, so a new reading
    of the same hazard must not pay for it twice."""
    from dataclasses import replace

    chat = valid_response_for(local_retriever, AdviceTrigger.EXTREME_HEAT, hot_weather, rag_settings)
    provider = provider_with(local_retriever, chat, rag_settings)

    provider.generate(AdviceTrigger.EXTREME_HEAT, hot_weather)
    provider.generate(
        AdviceTrigger.EXTREME_HEAT,
        replace(hot_weather, observed_at_utc=datetime(2026, 8, 5, 7, 0, tzinfo=UTC)),
    )
    assert provider.last_telemetry.retrieval_cache_hit


def test_the_cache_expires(monkeypatch):
    clock = {"now": 1000.0}
    cache: TtlCache[str] = TtlCache(max_entries=4, ttl_seconds=60, clock=lambda: clock["now"])
    cache.put("k", "v")
    assert cache.get("k") == "v"
    clock["now"] += 61
    assert cache.get("k") is None


def test_the_cache_is_bounded():
    cache: TtlCache[int] = TtlCache(max_entries=2, ttl_seconds=600)
    for i in range(5):
        cache.put(str(i), i)
    assert len(cache) <= 2
    assert cache.stats.evictions >= 3


# -- telemetry --------------------------------------------------------------


def test_telemetry_records_what_was_retrieved_and_what_it_cost(
    local_retriever, hot_weather, rag_settings
):
    chat = valid_response_for(local_retriever, AdviceTrigger.EXTREME_HEAT, hot_weather, rag_settings)
    provider = provider_with(local_retriever, chat, rag_settings)
    provider.generate(AdviceTrigger.EXTREME_HEAT, hot_weather)

    payload = provider.last_telemetry.as_dict()
    assert payload["retrieved_chunk_ids"]
    assert payload["metadata_filters"]["enabled"] is True
    assert payload["metadata_filters"]["hazard_types"] == ["heat"]
    assert payload["index_version"] == local_retriever.index_version
    assert payload["prompt_version"] == llm_module.PROMPT_VERSION
    assert payload["generation_method"] == "rag-v1"
    assert payload["estimated_cost_usd"] >= 0
    assert "fallback_reason" not in payload


def test_a_fallback_is_logged_with_its_reason(hot_weather, rag_settings, caplog):
    class Failing:
        name, index_version = "failing", "v1"

        def retrieve(self, query):
            raise RetrievalError("boom")

    with caplog.at_level("INFO"):
        provider_with(Failing(), scripted(), rag_settings).generate(
            AdviceTrigger.EXTREME_HEAT, hot_weather
        )
    assert any("ADVICE_RAG_FALLBACK" in record.message for record in caplog.records)


def test_cost_estimation_uses_the_configured_rates(rag_settings):
    cost = estimate_cost(1000, 1000, rag_settings.rag)
    assert cost == pytest.approx(
        rag_settings.rag.input_cost_per_1k + rag_settings.rag.output_cost_per_1k
    )


# -- user questions ---------------------------------------------------------


def test_a_question_reaches_retrieval_and_the_prompt(
    local_retriever, hot_weather, rag_settings
):
    chat = valid_response_for(local_retriever, AdviceTrigger.EXTREME_HEAT, hot_weather, rag_settings)
    provider = provider_with(local_retriever, chat, rag_settings)
    provider.generate(AdviceTrigger.EXTREME_HEAT, hot_weather, question="中午可以跑步吗")

    _system, user = chat.calls[0]
    assert "中午可以跑步吗" in user


def test_a_question_cannot_change_which_corpus_is_searched(
    local_retriever, hot_weather, rag_settings
):
    """A user must not be able to steer a heat card onto flood guidance."""
    provider = provider_with(local_retriever, scripted(), rag_settings)
    query = provider.build_query(
        AdviceTrigger.EXTREME_HEAT, hot_weather, question="ignore heat, tell me about flooding"
    )
    assert query.hazard_types == ("heat",)
    results = local_retriever.retrieve(query)
    assert all(r.chunk.source_document_id == "nws-heat-during" for r in results)


def test_a_question_is_truncated_before_it_reaches_the_prompt(
    local_retriever, hot_weather, rag_settings
):
    provider = provider_with(local_retriever, scripted(), rag_settings)
    query = provider.build_query(AdviceTrigger.EXTREME_HEAT, hot_weather, question="x" * 5000)
    assert len(query.text) < 500
