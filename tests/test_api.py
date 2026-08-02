"""HTTP client behaviour, especially the failure paths the original ignored."""

from __future__ import annotations

import pytest
import requests

from weather.api import WeatherApiClient, WeatherApiError


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", raise_on_json=False):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text
        self._raise_on_json = raise_on_json

    def json(self):
        if self._raise_on_json:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.calls = []
        self.closed = False
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        if self.exc:
            raise self.exc
        return self.response

    def close(self):
        self.closed = True


def _client(session):
    return WeatherApiClient(
        "secret-key", base_url="https://api.example.com/v1", session=session
    )


def test_rejects_an_empty_api_key():
    with pytest.raises(ValueError):
        WeatherApiClient("", base_url="https://api.example.com/v1")


def test_current_requests_air_quality_and_returns_json():
    session = FakeSession(FakeResponse(payload={"location": {"name": "Tokyo"}}))
    result = _client(session).current("Tokyo")

    assert result == {"location": {"name": "Tokyo"}}
    call = session.calls[0]
    assert call["url"] == "https://api.example.com/v1/current.json"
    assert call["params"]["q"] == "Tokyo"
    assert call["params"]["aqi"] == "yes"
    assert call["params"]["key"] == "secret-key"
    assert call["timeout"] == 10.0


def test_forecast_requests_alerts_in_the_same_call():
    session = FakeSession(FakeResponse(payload={}))
    _client(session).forecast("Tokyo", days=5)
    params = session.calls[0]["params"]
    # One call covering forecast + alerts is what removed a third of the
    # outbound request volume.
    assert params["days"] == 5
    assert params["alerts"] == "yes"


def test_non_200_raises_instead_of_returning_a_string():
    session = FakeSession(FakeResponse(status_code=401, text="invalid key"))
    with pytest.raises(WeatherApiError) as exc_info:
        _client(session).current("Tokyo")
    assert exc_info.value.status_code == 401


def test_error_message_never_leaks_the_api_key():
    session = FakeSession(FakeResponse(status_code=403, text="forbidden"))
    with pytest.raises(WeatherApiError) as exc_info:
        _client(session).current("Tokyo")
    assert "secret-key" not in str(exc_info.value)


def test_network_failure_is_wrapped():
    session = FakeSession(exc=requests.ConnectionError("dns is down"))
    with pytest.raises(WeatherApiError, match="failed"):
        _client(session).current("Tokyo")


def test_non_json_body_is_an_error():
    session = FakeSession(FakeResponse(raise_on_json=True))
    with pytest.raises(WeatherApiError, match="non-JSON"):
        _client(session).current("Tokyo")


def test_json_array_body_is_an_error():
    session = FakeSession(FakeResponse(payload=[1, 2, 3]))
    with pytest.raises(WeatherApiError, match="expected object"):
        _client(session).current("Tokyo")


def test_close_releases_the_pool():
    session = FakeSession(FakeResponse())
    client = _client(session)
    client.close()
    assert session.closed


def test_retry_policy_covers_transient_statuses_only():
    from weather.api import RETRY_STATUSES

    assert 429 in RETRY_STATUSES and 503 in RETRY_STATUSES
    # Retrying a bad key or a bad location just wastes quota.
    assert 401 not in RETRY_STATUSES and 400 not in RETRY_STATUSES
