"""Canonical record schemas for everything that leaves this application.

Design notes that matter downstream:

* Every record carries ``schema_version`` so the lake can hold several
  generations of the format at once and readers can branch on it.
* Every record carries a deterministic ``record_id``. The source API refreshes
  observations roughly every 10-15 minutes while we poll every 30 seconds, so
  the stream contains a lot of repeats by design. A stable id derived from
  (location, observation time) lets the curation step de-duplicate without
  keeping any state — the same trick used to collapse repeated syslog lines.
* ``ingested_at_utc`` (when we saw it) is kept separate from
  ``observed_at_utc`` (when the reading was taken), because they diverge and
  conflating them makes late-arriving data impossible to reason about.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

SCHEMA_VERSION = "1.0"
SOURCE_NAME = "weatherapi.com"


def utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None


def _epoch_to_utc(epoch: int | None) -> datetime | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=UTC)


def make_record_id(*parts: Any) -> str:
    """Deterministic id from the natural key of a record."""
    joined = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class Location:
    name: str | None = None
    region: str | None = None
    country: str | None = None
    lat: float | None = None
    lon: float | None = None
    tz_id: str | None = None
    localtime: str | None = None

    @property
    def key(self) -> str:
        """Stable identifier for a location, independent of display name casing."""
        if self.lat is not None and self.lon is not None:
            return f"{self.lat:.4f},{self.lon:.4f}"
        return (self.name or "unknown").strip().lower()


@dataclass(frozen=True)
class AirQuality:
    co: float | None = None
    no2: float | None = None
    o3: float | None = None
    so2: float | None = None
    pm2_5: float | None = None
    pm10: float | None = None
    us_epa_index: int | None = None
    gb_defra_index: int | None = None


@dataclass(frozen=True)
class CurrentWeatherRecord:
    """One observation of current conditions for one location."""

    record_id: str
    location_key: str
    location: Location
    observed_at_utc: str | None
    ingested_at_utc: str
    temp_c: float | None = None
    feelslike_c: float | None = None
    is_day: int | None = None
    condition_text: str | None = None
    condition_icon: str | None = None
    wind_kph: float | None = None
    wind_degree: int | None = None
    wind_dir: str | None = None
    pressure_mb: float | None = None
    precip_mm: float | None = None
    humidity: int | None = None
    cloud: int | None = None
    uv: float | None = None
    air_quality: AirQuality = field(default_factory=AirQuality)
    schema_version: str = SCHEMA_VERSION
    source: str = SOURCE_NAME
    record_type: str = "current"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def flatten(self) -> dict[str, Any]:
        """Single-level dict, for Parquet/CSV/Power BI where nesting is a nuisance."""
        row = self.to_dict()
        location = row.pop("location")
        air_quality = row.pop("air_quality")
        row.update({f"location_{k}": v for k, v in location.items()})
        row.update({f"aqi_{k}": v for k, v in air_quality.items()})
        return row


@dataclass(frozen=True)
class ForecastDayRecord:
    record_id: str
    location_key: str
    location: Location
    date: str | None
    ingested_at_utc: str
    maxtemp_c: float | None = None
    mintemp_c: float | None = None
    avgtemp_c: float | None = None
    maxwind_kph: float | None = None
    totalprecip_mm: float | None = None
    avghumidity: float | None = None
    daily_chance_of_rain: int | None = None
    uv: float | None = None
    condition_text: str | None = None
    schema_version: str = SCHEMA_VERSION
    source: str = SOURCE_NAME
    record_type: str = "forecast"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ForecastHourRecord:
    """One hour of the forecast for one location.

    Added for the advice engine: a "will it rain in the next hour" rule needs
    hourly precipitation probability, and the daily forecast only carries
    `daily_chance_of_rain`, which answers a different question.
    """

    record_id: str
    location_key: str
    location: Location
    time_utc: str | None
    ingested_at_utc: str
    temp_c: float | None = None
    precip_mm: float | None = None
    chance_of_rain: int | None = None
    wind_kph: float | None = None
    condition_text: str | None = None
    schema_version: str = SCHEMA_VERSION
    source: str = SOURCE_NAME
    record_type: str = "forecast_hour"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AlertRecord:
    record_id: str
    location_key: str
    location: Location
    ingested_at_utc: str
    headline: str | None = None
    severity: str | None = None
    event: str | None = None
    effective: str | None = None
    expires: str | None = None
    description: str | None = None
    instruction: str | None = None
    schema_version: str = SCHEMA_VERSION
    source: str = SOURCE_NAME
    record_type: str = "alert"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ThresholdBreach:
    """A reading that crossed an operational threshold — the 'monitoring' output."""

    record_id: str
    location_key: str
    metric: str
    value: float
    threshold: float
    comparison: str
    severity: str
    observed_at_utc: str | None
    detected_at_utc: str
    message: str
    schema_version: str = SCHEMA_VERSION
    record_type: str = "threshold_breach"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
