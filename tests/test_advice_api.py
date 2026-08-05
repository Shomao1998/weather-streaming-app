"""The advice HTTP surface: status codes, headers, and degradation.

The contract that matters most here is negative: nothing this endpoint does
may break the weather endpoints the dashboard renders first.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import azure.functions as func
import pytest

from weather.advice import repository as advice_repository


@pytest.fixture(autouse=True)
def advice_env(monkeypatch):
    monkeypatch.setenv("WEATHER_API_KEY", "local-test-key")
    monkeypatch.setenv("EVENT_HUB_ENABLED", "false")
    monkeypatch.setenv("STORAGE_ENABLED", "true")
    monkeypatch.setenv("STORAGE_CONNECTION_STRING", "UseDevelopmentStorage=true")
    from weather.config import get_settings

    get_settings.cache_clear()
    advice_repository.reset_for_tests()
    # Never let an API test reach a storage account.
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


def post_request(payload) -> func.HttpRequest:
    return func.HttpRequest(
        method="POST",
        url="http://localhost/api/advice/feedback",
        params={},
        headers={"Content-Type": "application/json"},
        body=json.dumps(payload).encode("utf-8"),
    )


class TestAdviceEndpoint:
    def test_200_with_a_card_when_a_rule_matches(self, app_module, monkeypatch):
        monkeypatch.setattr(
            app_module.serving, "read_serving_document",
            lambda *a, **k: make_snapshot(precip_chance_next_hour=95),
        )
        response = app_module.api_advice(get_request(location="Tokyo"))

        assert response.status_code == 200
        card = json.loads(response.get_body())
        assert card["trigger_code"] == "RAIN_EXPECTED"
        assert card["generation_method"] == "template-v1"
        assert response.headers["ETag"] == f'"{card["recommendation_id"]}"'

    def test_204_when_no_rule_matches(self, app_module, monkeypatch):
        monkeypatch.setattr(
            app_module.serving, "read_serving_document", lambda *a, **k: make_snapshot()
        )
        response = app_module.api_advice(get_request(location="Tokyo"))
        assert response.status_code == 204
        assert response.headers["X-Advice-Outcome"] == "no_rule_matched"

    def test_204_when_the_weather_is_stale(self, app_module, monkeypatch):
        old = (datetime.now(UTC) - timedelta(hours=6)).isoformat().replace("+00:00", "Z")
        monkeypatch.setattr(
            app_module.serving, "read_serving_document",
            lambda *a, **k: make_snapshot(precip_chance_next_hour=95, observed_at_utc=old),
        )
        response = app_module.api_advice(get_request(location="Tokyo"))
        assert response.status_code == 204
        assert response.headers["X-Advice-Outcome"] == "stale_weather"

    def test_400_when_location_is_missing(self, app_module):
        response = app_module.api_advice(get_request())
        assert response.status_code == 400
        assert "location" in json.loads(response.get_body())["error"]

    def test_400_when_the_location_is_unknown(self, app_module, monkeypatch):
        monkeypatch.setattr(
            app_module.serving, "read_serving_document", lambda *a, **k: make_snapshot()
        )
        response = app_module.api_advice(get_request(location="Atlantis"))
        assert response.status_code == 400
        assert "Atlantis" in json.loads(response.get_body())["error"]

    def test_204_when_the_snapshot_has_not_been_curated_yet(self, app_module, monkeypatch):
        def unavailable(*args, **kwargs):
            raise app_module.serving.ServingDataUnavailable("not generated yet")

        monkeypatch.setattr(app_module.serving, "read_serving_document", unavailable)
        assert app_module.api_advice(get_request(location="Tokyo")).status_code == 204

    def test_an_unexpected_failure_degrades_to_204(self, app_module, monkeypatch):
        def explode(*args, **kwargs):
            raise RuntimeError("storage on fire")

        monkeypatch.setattr(app_module.serving, "read_serving_document", explode)
        assert app_module.api_advice(get_request(location="Tokyo")).status_code == 204

    def test_a_session_id_is_always_returned(self, app_module, monkeypatch):
        monkeypatch.setattr(
            app_module.serving, "read_serving_document", lambda *a, **k: make_snapshot()
        )
        response = app_module.api_advice(get_request(location="Tokyo"))
        assert response.headers["X-Advice-Session"]

    def test_repeated_calls_return_the_same_card_id_for_one_observation(
        self, app_module, monkeypatch
    ):
        snapshot = make_snapshot(precip_chance_next_hour=95)
        monkeypatch.setattr(
            app_module.serving, "read_serving_document", lambda *a, **k: snapshot
        )
        first = app_module.api_advice(get_request(location="Tokyo", session="s1"))
        card = json.loads(first.get_body())
        # Same session is now frequency-limited; a different one still gets the
        # identical id, because the id is a function of the observation.
        second = app_module.api_advice(get_request(location="Tokyo", session="s2"))
        assert json.loads(second.get_body())["recommendation_id"] == card["recommendation_id"]


class TestIsolationFromTheWeatherApi:
    def test_a_broken_advice_service_leaves_api_latest_working(self, app_module, monkeypatch):
        payload = make_snapshot()

        monkeypatch.setattr(
            app_module.serving, "read_serving_document", lambda *a, **k: payload
        )

        def explode(*args, **kwargs):
            raise RuntimeError("advice is broken")

        monkeypatch.setattr(app_module, "AdviceService", explode)

        weather = app_module.api_latest(
            func.HttpRequest("GET", "http://localhost/api/latest", params={}, headers={}, body=b"")
        )
        assert weather.status_code == 200
        assert json.loads(weather.get_body())["locations"][0]["name"] == "Tokyo"

        advice = app_module.api_advice(get_request(location="Tokyo"))
        assert advice.status_code == 204


class TestFeedbackEndpoint:
    def test_accepts_a_valid_event(self, app_module):
        response = app_module.api_advice_feedback(
            post_request(
                {
                    "event": "helpful",
                    "session_id": "s1",
                    "trigger_code": "RAIN_EXPECTED",
                    "recommendation_id": "rec-1",
                    "location": "Tokyo",
                }
            )
        )
        assert response.status_code == 202

    def test_rejects_an_unknown_event(self, app_module):
        response = app_module.api_advice_feedback(
            post_request({"event": "nope", "session_id": "s1"})
        )
        assert response.status_code == 400

    def test_rejects_a_non_json_body(self, app_module):
        request = func.HttpRequest(
            method="POST",
            url="http://localhost/api/advice/feedback",
            params={},
            headers={},
            body=b"not json",
        )
        assert app_module.api_advice_feedback(request).status_code == 400

    def test_rejects_a_json_array(self, app_module):
        request = func.HttpRequest(
            method="POST",
            url="http://localhost/api/advice/feedback",
            params={},
            headers={"Content-Type": "application/json"},
            body=b"[1, 2, 3]",
        )
        assert app_module.api_advice_feedback(request).status_code == 400

    def test_a_missing_session_is_filled_in_rather_than_rejected(self, app_module):
        response = app_module.api_advice_feedback(
            post_request({"event": "shown", "trigger_code": "HIGH_UV"})
        )
        assert response.status_code == 202
