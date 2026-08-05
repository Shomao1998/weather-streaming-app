"""Raw API payloads -> canonical records.

Pure functions only: no network, no clock beyond an injectable ``ingested_at``,
no Azure SDK. That is what makes this layer testable against saved fixtures.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from .models import (
    AirQuality,
    AlertRecord,
    CurrentWeatherRecord,
    ForecastDayRecord,
    ForecastHourRecord,
    Location,
    _epoch_to_utc,
    _iso,
    make_record_id,
    utcnow,
)


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    number = _as_float(value)
    return None if number is None else int(number)


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _dict(payload: Any, *keys: str) -> dict[str, Any]:
    """Walk nested keys, returning {} rather than raising on any missing level."""
    node: Any = payload
    for key in keys:
        if not isinstance(node, dict):
            return {}
        node = node.get(key)
    return node if isinstance(node, dict) else {}


def _list(payload: Any, *keys: str) -> list[Any]:
    node: Any = payload
    for key in keys:
        if not isinstance(node, dict):
            return []
        node = node.get(key)
    return node if isinstance(node, list) else []


def parse_location(payload: dict[str, Any]) -> Location:
    raw = _dict(payload, "location")
    return Location(
        name=_as_str(raw.get("name")),
        region=_as_str(raw.get("region")),
        country=_as_str(raw.get("country")),
        lat=_as_float(raw.get("lat")),
        lon=_as_float(raw.get("lon")),
        tz_id=_as_str(raw.get("tz_id")),
        localtime=_as_str(raw.get("localtime")),
    )


def parse_air_quality(raw: dict[str, Any]) -> AirQuality:
    return AirQuality(
        co=_as_float(raw.get("co")),
        no2=_as_float(raw.get("no2")),
        o3=_as_float(raw.get("o3")),
        so2=_as_float(raw.get("so2")),
        pm2_5=_as_float(raw.get("pm2_5")),
        pm10=_as_float(raw.get("pm10")),
        # Hyphenated keys are renamed here; downstream columns must be valid
        # identifiers for Parquet and Power BI.
        us_epa_index=_as_int(raw.get("us-epa-index")),
        gb_defra_index=_as_int(raw.get("gb-defra-index")),
    )


def to_current_record(
    payload: dict[str, Any],
    *,
    ingested_at: datetime | None = None,
) -> CurrentWeatherRecord:
    """Build one current-conditions record from a ``current.json`` response."""
    location = parse_location(payload)
    current = _dict(payload, "current")
    condition = _dict(current, "condition")

    observed_at = _epoch_to_utc(_as_int(current.get("last_updated_epoch")))
    ingested = ingested_at or utcnow()

    return CurrentWeatherRecord(
        # Natural key: a location plus the moment the upstream reading was
        # taken. Polling faster than the source refreshes yields the same id,
        # which is exactly how duplicates get collapsed later.
        record_id=make_record_id(
            "current", location.key, current.get("last_updated_epoch")
        ),
        location_key=location.key,
        location=location,
        observed_at_utc=_iso(observed_at),
        ingested_at_utc=_iso(ingested),
        temp_c=_as_float(current.get("temp_c")),
        feelslike_c=_as_float(current.get("feelslike_c")),
        is_day=_as_int(current.get("is_day")),
        condition_text=_as_str(condition.get("text")),
        condition_icon=_as_str(condition.get("icon")),
        wind_kph=_as_float(current.get("wind_kph")),
        wind_degree=_as_int(current.get("wind_degree")),
        wind_dir=_as_str(current.get("wind_dir")),
        # Metric units throughout; the original mixed pressure_in/precip_in
        # with temp_c, which made the dashboard axes meaningless.
        pressure_mb=_as_float(current.get("pressure_mb")),
        precip_mm=_as_float(current.get("precip_mm")),
        humidity=_as_int(current.get("humidity")),
        cloud=_as_int(current.get("cloud")),
        uv=_as_float(current.get("uv")),
        air_quality=parse_air_quality(_dict(current, "air_quality")),
    )


def to_forecast_records(
    payload: dict[str, Any],
    *,
    ingested_at: datetime | None = None,
) -> list[ForecastDayRecord]:
    """One record per forecast day from a ``forecast.json`` response."""
    location = parse_location(payload)
    ingested = _iso(ingested_at or utcnow())
    records: list[ForecastDayRecord] = []

    for entry in _list(payload, "forecast", "forecastday"):
        if not isinstance(entry, dict):
            continue
        date = _as_str(entry.get("date"))
        day = entry.get("day") if isinstance(entry.get("day"), dict) else {}
        condition = _dict(day, "condition")
        records.append(
            ForecastDayRecord(
                record_id=make_record_id("forecast", location.key, date),
                location_key=location.key,
                location=location,
                date=date,
                ingested_at_utc=ingested,
                maxtemp_c=_as_float(day.get("maxtemp_c")),
                mintemp_c=_as_float(day.get("mintemp_c")),
                avgtemp_c=_as_float(day.get("avgtemp_c")),
                maxwind_kph=_as_float(day.get("maxwind_kph")),
                totalprecip_mm=_as_float(day.get("totalprecip_mm")),
                avghumidity=_as_float(day.get("avghumidity")),
                daily_chance_of_rain=_as_int(day.get("daily_chance_of_rain")),
                uv=_as_float(day.get("uv")),
                condition_text=_as_str(condition.get("text")),
            )
        )
    return records


def to_forecast_hour_records(
    payload: dict[str, Any],
    *,
    ingested_at: datetime | None = None,
    hours_ahead: int = 6,
) -> list[ForecastHourRecord]:
    """Forecast hours within the look-ahead window, one record each.

    The daily forecast cannot answer "is it about to rain", so the hourly
    breakdown the same response already contains is kept as its own record
    type — but only the next few hours of it. The response holds every hour of
    every requested day; keeping all of them would quadruple ingest volume to
    serve a rule that never looks past the next hour.
    """
    location = parse_location(payload)
    now = ingested_at or utcnow()
    ingested = _iso(now)
    horizon = now + timedelta(hours=hours_ahead)
    records: list[ForecastHourRecord] = []

    for day in _list(payload, "forecast", "forecastday"):
        if not isinstance(day, dict):
            continue
        for entry in day.get("hour") or []:
            if not isinstance(entry, dict):
                continue
            moment = _epoch_to_utc(_as_int(entry.get("time_epoch")))
            floor = now.replace(minute=0, second=0, microsecond=0)
            if moment is None or moment < floor or moment > horizon:
                continue
            time_utc = _iso(moment)
            condition = _dict(entry, "condition")
            records.append(
                ForecastHourRecord(
                    record_id=make_record_id("forecast_hour", location.key, time_utc),
                    location_key=location.key,
                    location=location,
                    time_utc=time_utc,
                    ingested_at_utc=ingested,
                    temp_c=_as_float(entry.get("temp_c")),
                    precip_mm=_as_float(entry.get("precip_mm")),
                    chance_of_rain=_as_int(entry.get("chance_of_rain")),
                    wind_kph=_as_float(entry.get("wind_kph")),
                    condition_text=_as_str(condition.get("text")),
                )
            )
    return records


def to_alert_records(
    payload: dict[str, Any],
    *,
    ingested_at: datetime | None = None,
) -> list[AlertRecord]:
    """One record per active alert. Works on both alerts.json and forecast.json."""
    location = parse_location(payload)
    ingested = _iso(ingested_at or utcnow())
    records: list[AlertRecord] = []

    for entry in _list(payload, "alerts", "alert"):
        if not isinstance(entry, dict):
            continue
        headline = _as_str(entry.get("headline"))
        effective = _as_str(entry.get("effective"))
        records.append(
            AlertRecord(
                record_id=make_record_id("alert", location.key, headline, effective),
                location_key=location.key,
                location=location,
                ingested_at_utc=ingested,
                headline=headline,
                severity=_as_str(entry.get("severity")),
                event=_as_str(entry.get("event")),
                effective=effective,
                expires=_as_str(entry.get("expires")),
                description=_as_str(entry.get("desc")),
                instruction=_as_str(entry.get("instruction")),
            )
        )
    return records


def deduplicate(records: Iterable[Any]) -> list[Any]:
    """Keep the first occurrence of each ``record_id``, preserving order."""
    seen: set[str] = set()
    unique: list[Any] = []
    for record in records:
        rid = getattr(record, "record_id", None)
        if rid is None or rid in seen:
            continue
        seen.add(rid)
        unique.append(record)
    return unique
