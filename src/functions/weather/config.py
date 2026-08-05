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
    # The forecast response carries every hour of every requested day — 72
    # records per location per poll. The advice engine only ever looks at the
    # next hour, so the rest is volume nobody reads: at a 30-minute cadence it
    # would quadruple what lands in bronze.
    forecast_hours_ahead: int = 6
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
class AdviceSettings:
    """Thresholds and policy for the advice cards.

    Every number a rule compares against lives here, so the rule engine reads
    like the business logic it is and a threshold change never means editing a
    conditional.

    These are deliberately separate from `MonitoringSettings`: an operational
    alert ("page someone") and a piece of advice ("take an umbrella") fire at
    different levels, and coupling them would force one to move whenever the
    other did.
    """

    enabled: bool = True
    # Bumping this invalidates every deduplication key, which is how a copy or
    # threshold change is allowed to reach users who already saw the old card.
    rule_version: str = "2026-08-04"

    # A card claims to describe the weather now, so it must refuse to be built
    # on an observation that is no longer "now". The serving layer is curated
    # hourly, so this has to be wider than that or nothing would ever qualify.
    max_weather_age_minutes: int = 90

    rain_chance_percent: int = 80
    uv_index: float = 8.0
    heat_c: float = 35.0
    wind_kph: float = 40.0

    card_ttl_minutes: int = 60
    min_interval_minutes: int = 180
    # A muted category stays muted for the rest of the UTC day.
    mute_rest_of_day: bool = True


@dataclass(frozen=True)
class Settings:
    weather: WeatherApiSettings = field(default_factory=WeatherApiSettings)
    event_hub: EventHubSettings = field(default_factory=EventHubSettings)
    storage: StorageSettings = field(default_factory=StorageSettings)
    monitoring: MonitoringSettings = field(default_factory=MonitoringSettings)
    advice: AdviceSettings = field(default_factory=AdviceSettings)
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
            forecast_hours_ahead=_int("WEATHER_FORECAST_HOURS_AHEAD", 6),
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
        advice=AdviceSettings(
            enabled=_bool("ADVICE_ENABLED", True),
            rule_version=_optional("ADVICE_RULE_VERSION", "2026-08-04"),
            max_weather_age_minutes=_int("ADVICE_MAX_WEATHER_AGE_MINUTES", 90),
            rain_chance_percent=_int("ADVICE_RAIN_CHANCE_PERCENT", 80),
            uv_index=float(_int("ADVICE_UV_INDEX", 8)),
            heat_c=float(_int("ADVICE_HEAT_C", 35)),
            wind_kph=float(_int("ADVICE_WIND_KPH", 40)),
            card_ttl_minutes=_int("ADVICE_CARD_TTL_MINUTES", 60),
            min_interval_minutes=_int("ADVICE_MIN_INTERVAL_MINUTES", 180),
            mute_rest_of_day=_bool("ADVICE_MUTE_REST_OF_DAY", True),
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
