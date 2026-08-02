"""HTTP client for weatherapi.com.

Wraps the three endpoints we consume with a shared connection pool, explicit
timeouts and bounded retries. The old implementation created a new connection
per call and turned HTTP errors into strings that blew up two lines later; here
a failure is a typed exception the caller can decide what to do with.
"""

from __future__ import annotations

import logging
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# 429 = rate limited, 5xx = transient upstream trouble. Anything else (401 bad
# key, 400 bad location) is our bug and retrying only hides it.
RETRY_STATUSES = (429, 500, 502, 503, 504)


class WeatherApiError(RuntimeError):
    """The weather API could not be queried successfully."""

    def __init__(self, message: str, *, status_code: int | None = None, body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def _build_session(max_retries: int) -> requests.Session:
    retry = Retry(
        total=max_retries,
        connect=max_retries,
        read=max_retries,
        status=max_retries,
        backoff_factor=0.5,  # 0.5s, 1s, 2s
        status_forcelist=RETRY_STATUSES,
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": "weather-streaming-app/1.0"})
    return session


class WeatherApiClient:
    """Thin, reusable client. Create once per worker, not once per invocation."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
        session: requests.Session | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._session = session or _build_session(max_retries)

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}/{path.lstrip('/')}"
        query = {"key": self._api_key, **params}
        try:
            response = self._session.get(url, params=query, timeout=self._timeout)
        except requests.RequestException as exc:
            raise WeatherApiError(f"Request to {path} failed: {exc}") from exc

        if response.status_code != 200:
            # Never let the API key reach a log line or an exception message.
            raise WeatherApiError(
                f"{path} returned HTTP {response.status_code}",
                status_code=response.status_code,
                body=response.text[:500],
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise WeatherApiError(f"{path} returned a non-JSON body") from exc

        if not isinstance(payload, dict):
            raise WeatherApiError(f"{path} returned {type(payload).__name__}, expected object")
        return payload

    def current(self, location: str) -> dict[str, Any]:
        """Current conditions including air quality."""
        return self._get("current.json", {"q": location, "aqi": "yes"})

    def forecast(self, location: str, days: int = 3) -> dict[str, Any]:
        """Daily forecast. Also carries alerts, so one call covers both."""
        return self._get(
            "forecast.json",
            {"q": location, "days": days, "aqi": "yes", "alerts": "yes"},
        )

    def alerts(self, location: str) -> dict[str, Any]:
        """Weather alerts only. Kept for callers that do not need the forecast."""
        return self._get("alerts.json", {"q": location, "alerts": "yes"})

    def close(self) -> None:
        self._session.close()
