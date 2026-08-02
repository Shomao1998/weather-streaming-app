"""Configuration must fail loudly at startup, not silently at 3am."""

from __future__ import annotations

import pytest

from weather.config import ConfigError, load_settings


def _minimum_viable_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEATHER_API_KEY", "local-test-key")
    monkeypatch.setenv("EVENT_HUB_ENABLED", "false")
    monkeypatch.setenv("STORAGE_ENABLED", "true")
    monkeypatch.setenv("STORAGE_CONNECTION_STRING", "UseDevelopmentStorage=true")


def test_defaults_are_usable(monkeypatch):
    _minimum_viable_env(monkeypatch)
    settings = load_settings()
    assert settings.weather.locations == ("Tokyo",)
    assert settings.weather.forecast_days == 3
    assert settings.storage.bronze_container == "bronze"
    assert settings.monitoring.max_temp_c == 38.0


def test_locations_parse_as_a_list(monkeypatch):
    _minimum_viable_env(monkeypatch)
    monkeypatch.setenv("WEATHER_LOCATIONS", "Tokyo, Osaka ,Sapporo")
    assert load_settings().weather.locations == ("Tokyo", "Osaka", "Sapporo")


def test_rejects_every_sink_disabled(monkeypatch):
    _minimum_viable_env(monkeypatch)
    monkeypatch.setenv("STORAGE_ENABLED", "false")
    with pytest.raises(ConfigError, match="nowhere to go"):
        load_settings()


def test_rejects_event_hub_without_a_namespace(monkeypatch):
    _minimum_viable_env(monkeypatch)
    monkeypatch.setenv("EVENT_HUB_ENABLED", "true")
    with pytest.raises(ConfigError, match="EVENT_HUB_NAMESPACE"):
        load_settings()


def test_rejects_storage_without_an_endpoint(monkeypatch):
    _minimum_viable_env(monkeypatch)
    monkeypatch.delenv("STORAGE_CONNECTION_STRING")
    with pytest.raises(ConfigError, match="STORAGE_ACCOUNT_URL"):
        load_settings()


def test_rejects_missing_api_key_source(monkeypatch):
    monkeypatch.setenv("EVENT_HUB_ENABLED", "false")
    monkeypatch.setenv("STORAGE_CONNECTION_STRING", "UseDevelopmentStorage=true")
    with pytest.raises(ConfigError, match="weather API key"):
        load_settings()


def test_rejects_a_non_boolean_flag(monkeypatch):
    _minimum_viable_env(monkeypatch)
    monkeypatch.setenv("EVENT_HUB_ENABLED", "maybe")
    with pytest.raises(ConfigError, match="boolean"):
        load_settings()


def test_settings_never_hold_the_api_key_in_repr(monkeypatch):
    _minimum_viable_env(monkeypatch)
    monkeypatch.setenv("KEY_VAULT_URL", "https://kv-example.vault.azure.net/")
    monkeypatch.delenv("WEATHER_API_KEY")
    settings = load_settings()
    assert "local-test-key" not in repr(settings)
