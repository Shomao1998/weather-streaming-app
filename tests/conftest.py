"""Shared test setup.

The function app lives in ``src/functions`` so that the deployment package has
``host.json`` and ``function_app.py`` at its root; tests put that directory on
``sys.path`` to import the ``weather`` package the same way the runtime does.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FUNCTIONS_ROOT = REPO_ROOT / "src" / "functions"
FIXTURES = Path(__file__).parent / "fixtures"

sys.path.insert(0, str(FUNCTIONS_ROOT))


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def current_payload() -> dict:
    return load_fixture("current_tokyo.json")


@pytest.fixture
def forecast_payload() -> dict:
    return load_fixture("forecast_tokyo.json")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch):
    """Tests must never inherit real credentials or endpoints from the shell."""
    for name in (
        "WEATHER_API_KEY",
        "WEATHER_LOCATIONS",
        "WEATHER_FORECAST_DAYS",
        "KEY_VAULT_URL",
        "EVENT_HUB_ENABLED",
        "EVENT_HUB_NAMESPACE",
        "EVENT_HUB_NAME",
        "STORAGE_ENABLED",
        "STORAGE_ACCOUNT_URL",
        "STORAGE_CONNECTION_STRING",
        "APP_ENVIRONMENT",
    ):
        monkeypatch.delenv(name, raising=False)

    from weather import clients
    from weather.config import get_settings

    get_settings.cache_clear()
    clients.reset_for_tests()
    yield
    get_settings.cache_clear()
    clients.reset_for_tests()
