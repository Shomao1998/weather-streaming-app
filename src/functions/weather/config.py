"""Centralised configuration.

Every tunable lives here and comes from the environment, so the same code runs
locally (``local.settings.json``) and on Azure (App Settings) with no edits.
Nothing in this project reads ``os.environ`` outside this module.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

DEFAULT_BASE_URL = "https://api.weatherapi.com/v1"


class ConfigError(RuntimeError):
    """Raised when a required setting is missing or malformed."""


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"Required setting '{name}' is missing. "
            "Set it in local.settings.json (local) or App Settings (Azure)."
        )
    return value


def _optional(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = _optional(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"Setting '{name}' must be an integer, got {raw!r}.") from exc


def _bool(name: str, default: bool) -> bool:
    raw = _optional(name).lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"Setting '{name}' must be a boolean, got {raw!r}.")


def _csv(name: str, default: str) -> tuple[str, ...]:
    raw = _optional(name, default)
    items = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not items:
        raise ConfigError(f"Setting '{name}' resolved to an empty list.")
    return items


@dataclass(frozen=True)
class WeatherApiSettings:
    base_url: str = DEFAULT_BASE_URL
    locations: tuple[str, ...] = ("Tokyo",)
    forecast_days: int = 3
    timeout_seconds: float = 10.0
    max_retries: int = 3
    # The API key is resolved at call time (Key Vault or a local override), not
    # stored here, so the settings object stays safe to log.
    key_vault_url: str = ""
    api_key_secret_name: str = "weatherapi"
    api_key_override: str = ""


@dataclass(frozen=True)
class EventHubSettings:
    enabled: bool = True
    namespace: str = ""
    name: str = ""
    consumer_group: str = "$Default"


@dataclass(frozen=True)
class StorageSettings:
    enabled: bool = True
    account_url: str = ""
    connection_string: str = ""
    bronze_container: str = "bronze"
    silver_container: str = "silver"
    serving_container: str = "serving"


@dataclass(frozen=True)
class MonitoringSettings:
    # Thresholds that turn a reading into an operational event, mirroring the
    # "alert on critical syslog lines" requirement this project reproduces.
    max_temp_c: float = 38.0
    min_temp_c: float = -10.0
    max_wind_kph: float = 60.0
    max_pm2_5: float = 55.0
    max_us_epa_index: int = 4


@dataclass(frozen=True)
class Settings:
    weather: WeatherApiSettings = field(default_factory=WeatherApiSettings)
    event_hub: EventHubSettings = field(default_factory=EventHubSettings)
    storage: StorageSettings = field(default_factory=StorageSettings)
    monitoring: MonitoringSettings = field(default_factory=MonitoringSettings)
    environment: str = "local"

    def validate(self) -> None:
        """Fail fast on combinations that cannot possibly work at runtime."""
        if not self.event_hub.enabled and not self.storage.enabled:
            raise ConfigError(
                "Both EVENT_HUB_ENABLED and STORAGE_ENABLED are false — "
                "collected data would have nowhere to go."
            )
        if self.event_hub.enabled and not (self.event_hub.namespace and self.event_hub.name):
            raise ConfigError(
                "EVENT_HUB_ENABLED is true but EVENT_HUB_NAMESPACE / EVENT_HUB_NAME are unset."
            )
        if self.storage.enabled and not (
            self.storage.account_url or self.storage.connection_string
        ):
            raise ConfigError(
                "STORAGE_ENABLED is true but neither STORAGE_ACCOUNT_URL nor "
                "STORAGE_CONNECTION_STRING is set."
            )
        if not (self.weather.key_vault_url or self.weather.api_key_override):
            raise ConfigError(
                "No way to obtain the weather API key: set KEY_VAULT_URL (Azure) "
                "or WEATHER_API_KEY (local development)."
            )


def load_settings() -> Settings:
    """Build settings from the current environment. Not cached — see `get_settings`."""
    settings = Settings(
        weather=WeatherApiSettings(
            base_url=_optional("WEATHER_API_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            locations=_csv("WEATHER_LOCATIONS", "Tokyo"),
            forecast_days=_int("WEATHER_FORECAST_DAYS", 3),
            timeout_seconds=float(_int("WEATHER_API_TIMEOUT_SECONDS", 10)),
            max_retries=_int("WEATHER_API_MAX_RETRIES", 3),
            key_vault_url=_optional("KEY_VAULT_URL").rstrip("/"),
            api_key_secret_name=_optional("WEATHER_API_KEY_SECRET_NAME", "weatherapi"),
            api_key_override=_optional("WEATHER_API_KEY"),
        ),
        event_hub=EventHubSettings(
            enabled=_bool("EVENT_HUB_ENABLED", True),
            namespace=_optional("EVENT_HUB_NAMESPACE"),
            name=_optional("EVENT_HUB_NAME"),
            consumer_group=_optional("EVENT_HUB_CONSUMER_GROUP", "$Default"),
        ),
        storage=StorageSettings(
            enabled=_bool("STORAGE_ENABLED", True),
            account_url=_optional("STORAGE_ACCOUNT_URL").rstrip("/"),
            connection_string=_optional("STORAGE_CONNECTION_STRING"),
            bronze_container=_optional("STORAGE_BRONZE_CONTAINER", "bronze"),
            silver_container=_optional("STORAGE_SILVER_CONTAINER", "silver"),
            serving_container=_optional("STORAGE_SERVING_CONTAINER", "serving"),
        ),
        monitoring=MonitoringSettings(
            max_temp_c=float(_int("ALERT_MAX_TEMP_C", 38)),
            min_temp_c=float(_int("ALERT_MIN_TEMP_C", -10)),
            max_wind_kph=float(_int("ALERT_MAX_WIND_KPH", 60)),
            max_pm2_5=float(_int("ALERT_MAX_PM2_5", 55)),
            max_us_epa_index=_int("ALERT_MAX_US_EPA_INDEX", 4),
        ),
        environment=_optional("APP_ENVIRONMENT", "local"),
    )
    settings.validate()
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, resolved once per worker.

    Function workers are reused across invocations, so this avoids re-parsing
    the environment on every timer tick. Call `get_settings.cache_clear()` in
    tests after changing environment variables.
    """
    return load_settings()
