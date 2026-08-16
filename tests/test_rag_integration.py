"""RAG through the real HTTP surface, and the guarantees that must not move.

Two things are checked here that no unit test can check: that a card produced
by the retrieval path is still the *same shape* a phase-one client expects, and
that a broken knowledge layer changes nothing about the weather endpoints.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import azure.functions as func
import pytest

from weather.advice import factory
from weather.advice import repository as advice_repository
from weather.advice.embeddings import HashingEmbedder
from weather.advice.llm import ScriptedChatClient
from weather.advice.rag import RagAdviceProvider
from weather.advice.retrieval import LocalIndexRetriever, RetrievalError
from weather.config import RagSettings, Settings


@pytest.fixture(autouse=True)
def advice_env(monkeypatch):
    monkeypatch.setenv("WEATHER_API_KEY", "local-test-key")
    monkeypatch.setenv("EVENT_HUB_ENABLED", "false")
    monkeypatch.setenv("STORAGE_ENABLED", "true")
    monkeypatch.setenv("STORAGE_CONNECTION_STRING", "UseDevelopmentStorage=true")
    from weather.config import get_settings

    get_settings.cache_clear()
    advice_repository.reset_for_tests()
    monkeypatch.setattr(
        advice_repository, "get_repository",
        lambda settings=None: advice_repository.InMemoryAdviceRepository(),
    )
    yield
    get_settings.cache_clear()
    advice_repository.reset_for_tests()


@pytest.fixture
def app_module():
    import function_app

    return function_app


def make_snapshot(**overrides):
    now = datetime.now(UTC)
    entry = {
        "location_key": "35.6895,139.6917",
        "name": "Tokyo",
        "observed_at_utc": (now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "temp_c": 20.0,
        "uv": 2.0,
        "wind_kph": 5.0,
        "precip_chance_next_hour": 0,
    }
    entry.update(overrides)
    return {"generated_at_utc": now.isoformat(), "locations": [entry]}


def get_request(**params) -> func.HttpRequest:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return func.HttpRequest(
        method="GET",
        url=f"http://localhost/api/advice?{query}",
        params=params,
        headers={},
        body=b"",
    )


def rag_provider(knowledge_index, response: dict | None = None, chunk_filter=None):
    """A provider wired to the real corpus and a scripted model."""
    retriever = LocalIndexRetriever(knowledge_index, HashingEmbedder())
    settings = Settings(rag=RagSettings(enabled=True))
    if response is None:
        chunk = next(c for c in knowledge_index.chunks if (chunk_filter or (lambda _: True))(c))
        response = {
            "title": "降雨提醒",
            "message": "出门带伞，路上留出更多时间。",
            "advice_codes": ["CARRY_UMBRELLA", "ALLOW_EXTRA_TRAVEL_TIME"],
            "supporting_chunk_ids": [chunk.chunk_id],
        }
    chat = ScriptedChatClient([json.dumps(response, ensure_ascii=False)] * 6)
    provider = RagAdviceProvider(
        retriever=retriever, chat_client=chat, settings=settings
    )
    return provider, chat


def install(monkeypatch, provider):
    monkeypatch.setattr(factory, "get_provider", lambda settings=None: provider)


# -- the card, end to end ---------------------------------------------------


class TestGroundedCardOverHttp:
    def test_a_grounded_card_is_returned_with_its_sources(
        self, app_module, monkeypatch, knowledge_index
    ):
        rain_chunk = next(c for c in knowledge_index.chunks if "rain" in c.hazard_types)
        provider, _chat = rag_provider(knowledge_index, chunk_filter=lambda c: c is rain_chunk)
        install(monkeypatch, provider)
        monkeypatch.setattr(
            app_module.serving, "read_serving_document",
            lambda *a, **k: make_snapshot(precip_chance_next_hour=95),
        )

        response = app_module.api_advice(get_request(location="Tokyo"))

        assert response.status_code == 200
        card = json.loads(response.get_body())
        assert card["generation_method"] == "rag-v1"
        assert card["sources"], "a grounded card must ship its citations"
        assert card["sources"][0]["chunk_id"] == rain_chunk.chunk_id
        assert card["sources"][0]["source_url"].startswith("https://")
        assert card["advice_codes"] == ["CARRY_UMBRELLA", "ALLOW_EXTRA_TRAVEL_TIME"]

    def test_the_card_schema_is_unchanged_from_phase_one(
        self, app_module, monkeypatch, knowledge_index
    ):
        """A phase-one client must be able to read a phase-two card. Every
        field it knew about is still present, with the same meaning; the new
        fields are additive."""
        provider, _chat = rag_provider(knowledge_index)
        install(monkeypatch, provider)
        monkeypatch.setattr(
            app_module.serving, "read_serving_document",
            lambda *a, **k: make_snapshot(precip_chance_next_hour=95),
        )
        card = json.loads(app_module.api_advice(get_request(location="Tokyo")).get_body())

        for field in (
            "recommendation_id", "location", "trigger_code", "severity", "title",
            "message", "evidence", "actions", "generated_at_utc",
            "weather_observed_at_utc", "expires_at_utc", "generation_method",
            "weather_snapshot_id", "rule_version",
        ):
            assert field in card, field
        assert isinstance(card["evidence"], list)
        assert card["trigger_code"] == "RAIN_EXPECTED"
        # Observation time and generation time stay distinct, as in phase one.
        assert card["weather_observed_at_utc"] != card["generated_at_utc"]

    def test_the_rule_not_the_model_decides_severity_and_evidence(
        self, app_module, monkeypatch, knowledge_index
    ):
        """The model wrote the copy. It did not get to say how bad it is, or
        what figure the card displays."""
        provider, _chat = rag_provider(knowledge_index)
        install(monkeypatch, provider)
        monkeypatch.setattr(
            app_module.serving, "read_serving_document",
            lambda *a, **k: make_snapshot(precip_chance_next_hour=95),
        )
        card = json.loads(app_module.api_advice(get_request(location="Tokyo")).get_body())

        assert card["severity"] == "info"
        assert card["evidence"], "evidence comes from the rule, not the generated text"
        assert any("95" in str(item.get("value")) for item in card["evidence"])

    def test_a_question_is_passed_through_to_the_provider(
        self, app_module, monkeypatch, knowledge_index
    ):
        provider, chat = rag_provider(knowledge_index)
        install(monkeypatch, provider)
        monkeypatch.setattr(
            app_module.serving, "read_serving_document",
            lambda *a, **k: make_snapshot(precip_chance_next_hour=95),
        )

        app_module.api_advice(get_request(location="Tokyo", q="需要带伞吗"))

        assert chat.calls, "the model should have been asked"
        assert "需要带伞吗" in chat.calls[0][1]

    def test_an_over_long_question_is_rejected(self, app_module):
        response = app_module.api_advice(get_request(location="Tokyo", q="x" * 500))
        assert response.status_code == 400
        assert "200" in json.loads(response.get_body())["error"]

    def test_two_questions_get_different_etags(
        self, app_module, monkeypatch, knowledge_index
    ):
        provider, _chat = rag_provider(knowledge_index)
        install(monkeypatch, provider)
        monkeypatch.setattr(
            app_module.serving, "read_serving_document",
            lambda *a, **k: make_snapshot(precip_chance_next_hour=95),
        )
        first = app_module.api_advice(get_request(location="Tokyo", q="a"))
        second = app_module.api_advice(get_request(location="Tokyo", q="b"))
        assert first.headers["ETag"] != second.headers["ETag"]


# -- degradation ------------------------------------------------------------


class TestDegradation:
    def test_a_total_rag_failure_still_returns_a_card(
        self, app_module, monkeypatch, knowledge_index
    ):
        class Dead:
            name, index_version = "dead", "v1"

            def retrieve(self, query):
                raise RetrievalError("index unavailable")

        provider = RagAdviceProvider(
            retriever=Dead(),
            chat_client=ScriptedChatClient(["{}"]),
            settings=Settings(rag=RagSettings(enabled=True)),
        )
        install(monkeypatch, provider)
        monkeypatch.setattr(
            app_module.serving, "read_serving_document",
            lambda *a, **k: make_snapshot(precip_chance_next_hour=95),
        )

        response = app_module.api_advice(get_request(location="Tokyo"))

        assert response.status_code == 200
        card = json.loads(response.get_body())
        assert card["generation_method"] == "template-v1"
        assert card["title"] and card["message"]
        assert card["sources"] == []

    def test_a_provider_that_raises_does_not_break_the_endpoint(
        self, app_module, monkeypatch
    ):
        class Exploding:
            name = "exploding"

            def generate(self, trigger, weather, question=None):
                raise RuntimeError("provider is broken")

        install(monkeypatch, Exploding())
        monkeypatch.setattr(
            app_module.serving, "read_serving_document",
            lambda *a, **k: make_snapshot(precip_chance_next_hour=95),
        )
        response = app_module.api_advice(get_request(location="Tokyo"))
        assert response.status_code == 204
        assert response.headers["X-Advice-Outcome"] == "provider_failure"

    def test_the_weather_endpoints_are_untouched_by_a_broken_knowledge_layer(
        self, app_module, monkeypatch
    ):
        """The load-bearing guarantee: the dashboard renders weather first, and
        nothing about advice may reach it."""
        def explode(*args, **kwargs):
            raise RuntimeError("the whole knowledge layer is on fire")

        monkeypatch.setattr(factory, "get_provider", explode)
        monkeypatch.setattr(factory, "build_provider", explode)
        monkeypatch.setattr(
            app_module.serving, "read_serving_document", lambda *a, **k: make_snapshot()
        )

        for endpoint in (app_module.api_latest, app_module.api_timeseries, app_module.api_breaches):
            response = endpoint(get_request())
            assert response.status_code == 200
            assert json.loads(response.get_body())["locations"]

    def test_the_advice_endpoint_survives_a_broken_provider_factory(
        self, app_module, monkeypatch
    ):
        def explode(*args, **kwargs):
            raise RuntimeError("cannot build a provider")

        monkeypatch.setattr(factory, "get_provider", explode)
        monkeypatch.setattr(
            app_module.serving, "read_serving_document",
            lambda *a, **k: make_snapshot(precip_chance_next_hour=95),
        )
        response = app_module.api_advice(get_request(location="Tokyo"))
        assert response.status_code == 204


# -- provider selection -----------------------------------------------------


class TestProviderSelection:
    def test_an_unconfigured_deployment_gets_the_template_provider(self):
        provider = factory.build_provider(Settings())
        assert provider.name == "template-v1"

    def test_rag_enabled_without_a_model_gets_the_template_provider(self):
        settings = Settings(rag=RagSettings(enabled=True))
        assert factory.build_provider(settings).name == "template-v1"

    def test_a_missing_index_gets_the_template_provider(self):
        settings = Settings(
            rag=RagSettings(
                enabled=True,
                openai_endpoint="https://example.openai.azure.com",
                chat_deployment="gpt-test",
                index_path="knowledge/processed/does-not-exist.json",
            )
        )
        assert factory.build_provider(settings).name == "template-v1"

    def test_a_configured_deployment_gets_the_rag_provider(self):
        settings = Settings(
            rag=RagSettings(
                enabled=True,
                openai_endpoint="https://example.openai.azure.com",
                chat_deployment="gpt-test",
            )
        )
        assert factory.build_provider(settings).name == "rag-v1"

    def test_a_search_endpoint_selects_the_azure_retriever(self):
        settings = Settings(
            rag=RagSettings(enabled=True, search_endpoint="https://example.search.windows.net")
        )
        retriever = factory.build_retriever(settings)
        assert retriever.name == "azure-ai-search"

    def test_the_provider_is_built_once_per_process(self, monkeypatch):
        calls = []

        def counted(settings=None):
            calls.append(1)
            return factory.TemplateAdviceProvider()

        monkeypatch.setattr(factory, "build_provider", counted)
        settings = Settings()
        factory.get_provider(settings)
        factory.get_provider(settings)
        assert len(calls) == 1

    def test_a_configuration_change_rebuilds_the_provider(self, monkeypatch):
        calls = []

        def counted(settings=None):
            calls.append(1)
            return factory.TemplateAdviceProvider()

        monkeypatch.setattr(factory, "build_provider", counted)
        factory.get_provider(Settings())
        factory.get_provider(Settings(rag=RagSettings(enabled=True)))
        assert len(calls) == 2


# -- packaging --------------------------------------------------------------


class TestIndexResolution:
    """Where the index is found from, which differs between the deployed host
    and every local entry point."""

    def test_the_repository_index_resolves_from_any_working_directory(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        resolved = factory._resolve_index_path("knowledge/processed/index.json")
        assert resolved.exists(), "the committed index must resolve without the cwd"

    def test_an_index_inside_the_package_wins(self, monkeypatch, tmp_path):
        """CI stages the index into src/functions/ for deployment; that copy is
        the one the host must use."""
        import weather.advice.factory as factory_module

        package_root = Path(factory_module.__file__).resolve().parents[2]
        staged = package_root / "knowledge" / "processed" / "index.json"
        if staged.exists():  # pragma: no cover - only true in a built package
            assert factory._resolve_index_path("knowledge/processed/index.json") == staged
        else:
            # Not staged in a source checkout, so resolution falls through to
            # the repository root — which is the local development case.
            assert factory._resolve_index_path("knowledge/processed/index.json").exists()

    def test_an_absolute_path_is_used_as_given(self):
        assert factory._resolve_index_path("/nowhere/index.json") == Path("/nowhere/index.json")
