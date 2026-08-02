"""The work each Azure Function performs, expressed without any Functions API.

Keeping the orchestration here (rather than inside the trigger callbacks) means
every step can be exercised from a test or a script; `function_app.py` stays a
thin registration layer.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from . import clients, monitoring, sinks, transform
from .api import WeatherApiError
from .config import Settings, get_settings
from .models import CurrentWeatherRecord

logger = logging.getLogger(__name__)

SERVING_LATEST_BLOB = "latest.json"
SERVING_TIMESERIES_BLOB = "timeseries_24h.json"
SERVING_BREACHES_BLOB = "breaches_24h.json"


@dataclass
class IngestResult:
    """What one ingest run did — logged as a single structured line."""

    records_collected: int = 0
    records_written: int = 0
    breaches: int = 0
    failed_locations: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return not self.failed_locations

    def as_dict(self) -> dict[str, Any]:
        return {
            "records_collected": self.records_collected,
            "records_written": self.records_written,
            "breaches": self.breaches,
            "failed_locations": self.failed_locations,
        }


def ingest_current(settings: Settings | None = None) -> IngestResult:
    """Poll current conditions for every configured location and publish them."""
    settings = settings or get_settings()
    # Each of these can block on a network call the first time a worker runs
    # (Key Vault, then Event Hub). Logging the phase turns "the invocation
    # timed out after five minutes" into "it timed out resolving the secret".
    logger.info("ingest_current: resolving weather client")
    client = clients.get_weather_client(settings)
    logger.info("ingest_current: building sink")
    sink = sinks.build_ingest_sink(settings)
    logger.info("ingest_current: polling %d location(s)", len(settings.weather.locations))

    result = IngestResult()
    records: list[CurrentWeatherRecord] = []

    for location in settings.weather.locations:
        try:
            payload = client.current(location)
        except WeatherApiError as exc:
            # One bad location must not stop the others; the run is still
            # reported as degraded so the alert rule can fire.
            logger.error("Current fetch failed for %s: %s", location, exc)
            result.failed_locations.append(location)
            continue
        records.append(transform.to_current_record(payload))

    result.records_collected = len(records)

    breaches = [b for r in records for b in monitoring.evaluate(r, settings.monitoring)]
    monitoring.log_breaches(breaches)
    result.breaches = len(breaches)

    if records:
        result.records_written = sink.emit(records + breaches)

    summary = result.as_dict()
    logger.info("INGEST_CURRENT %s", summary, extra={"custom_dimensions": summary})
    return result


def ingest_forecast(settings: Settings | None = None) -> IngestResult:
    """Poll the slow-moving data: daily forecast and active alerts.

    One ``forecast.json`` call returns both, so this replaces two of the three
    calls the original made every 30 seconds.
    """
    settings = settings or get_settings()
    client = clients.get_weather_client(settings)
    sink = sinks.build_ingest_sink(settings)

    result = IngestResult()
    records: list[Any] = []

    for location in settings.weather.locations:
        try:
            payload = client.forecast(location, days=settings.weather.forecast_days)
        except WeatherApiError as exc:
            logger.error("Forecast fetch failed for %s: %s", location, exc)
            result.failed_locations.append(location)
            continue
        records.extend(transform.to_forecast_records(payload))
        records.extend(transform.to_alert_records(payload))

    result.records_collected = len(records)
    if records:
        result.records_written = sink.emit(records)

    summary = result.as_dict()
    logger.info("INGEST_FORECAST %s", summary, extra={"custom_dimensions": summary})
    return result


def archive_events(payloads: Iterable[dict[str, Any]], settings: Settings | None = None) -> int:
    """Persist a batch of Event Hub messages into the bronze layer.

    Event Hub retains data for days; the lake keeps it forever. Doing this in a
    function (rather than Event Hubs Capture) costs nothing extra and keeps the
    landing format as plain JSONL instead of Avro.
    """
    settings = settings or get_settings()
    if not settings.storage.enabled:
        logger.warning("Storage is disabled; dropping %s", "batch")
        return 0

    grouped: dict[str, list[dict[str, Any]]] = {}
    for payload in payloads:
        grouped.setdefault(payload.get("record_type", "unknown"), []).append(payload)

    blob_service = clients.get_blob_service(settings)
    written = 0
    for record_type, group in grouped.items():
        sink = sinks.BlobSink(
            blob_service=blob_service, container=settings.storage.bronze_container
        )
        written += sink.emit(group)
        logger.info("Archived %d %s record(s).", len(group), record_type)
    return written


def _bronze_prefixes(record_type: str, hours: int) -> list[str]:
    now = datetime.now(UTC)
    days = {(now - timedelta(hours=offset)).date() for offset in range(hours + 1)}
    return [f"{record_type}/date={day:%Y-%m-%d}/" for day in sorted(days)]


def read_bronze(
    record_type: str,
    hours: int = 24,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    """Load recent bronze records for one record type."""
    settings = settings or get_settings()
    blob_service = clients.get_blob_service(settings)
    container = blob_service.get_container_client(settings.storage.bronze_container)
    cutoff = datetime.now(UTC) - timedelta(hours=hours)

    rows: list[dict[str, Any]] = []
    for prefix in _bronze_prefixes(record_type, hours):
        for blob in container.list_blobs(name_starts_with=prefix):
            if blob.last_modified and blob.last_modified < cutoff:
                continue
            raw = container.download_blob(blob.name).readall()
            rows.extend(sinks.iter_jsonl(raw))
    return rows


def _dedupe_rows(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse the repeats created by polling faster than the source updates."""
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        rid = row.get("record_id")
        if rid and rid not in unique:
            unique[rid] = row
    return sorted(unique.values(), key=lambda r: r.get("observed_at_utc") or "")


def _flatten_row(row: dict[str, Any]) -> dict[str, Any]:
    flat = dict(row)
    for nested_key, prefix in (("location", "location_"), ("air_quality", "aqi_")):
        nested = flat.pop(nested_key, None)
        if isinstance(nested, dict):
            flat.update({f"{prefix}{k}": v for k, v in nested.items()})
    return flat


def build_serving_payloads(
    current_rows: Sequence[dict[str, Any]],
    breach_rows: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Shape the small JSON files the public dashboard reads.

    The dashboard is a static page with no backend, so the aggregation happens
    here, once an hour, instead of in the browser on every page load.
    """
    flat = [_flatten_row(row) for row in _dedupe_rows(current_rows)]
    generated_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    by_location: dict[str, list[dict[str, Any]]] = {}
    for row in flat:
        by_location.setdefault(row.get("location_key", "unknown"), []).append(row)

    latest = {
        "generated_at_utc": generated_at,
        "locations": [
            {
                "location_key": key,
                "name": rows[-1].get("location_name"),
                "country": rows[-1].get("location_country"),
                "observed_at_utc": rows[-1].get("observed_at_utc"),
                "temp_c": rows[-1].get("temp_c"),
                "feelslike_c": rows[-1].get("feelslike_c"),
                "condition_text": rows[-1].get("condition_text"),
                "condition_icon": rows[-1].get("condition_icon"),
                "humidity": rows[-1].get("humidity"),
                "wind_kph": rows[-1].get("wind_kph"),
                "wind_dir": rows[-1].get("wind_dir"),
                "pressure_mb": rows[-1].get("pressure_mb"),
                "uv": rows[-1].get("uv"),
                "pm2_5": rows[-1].get("aqi_pm2_5"),
                "us_epa_index": rows[-1].get("aqi_us_epa_index"),
                "observation_count_24h": len(rows),
            }
            for key, rows in sorted(by_location.items())
            if rows
        ],
    }

    timeseries = {
        "generated_at_utc": generated_at,
        "series": [
            {
                "location_key": key,
                "name": rows[-1].get("location_name"),
                "points": [
                    {
                        "t": row.get("observed_at_utc"),
                        "temp_c": row.get("temp_c"),
                        "humidity": row.get("humidity"),
                        "wind_kph": row.get("wind_kph"),
                        "pm2_5": row.get("aqi_pm2_5"),
                    }
                    for row in rows
                ],
            }
            for key, rows in sorted(by_location.items())
        ],
    }

    breaches = {
        "generated_at_utc": generated_at,
        "breaches": sorted(
            _dedupe_rows(breach_rows),
            key=lambda r: r.get("detected_at_utc") or "",
            reverse=True,
        )[:100],
    }

    return {
        SERVING_LATEST_BLOB: latest,
        SERVING_TIMESERIES_BLOB: timeseries,
        SERVING_BREACHES_BLOB: breaches,
    }


def _write_silver(rows: Sequence[dict[str, Any]], settings: Settings) -> str | None:
    """Write the curated table Power BI connects to.

    Parquet when pyarrow is importable, CSV otherwise — the pipeline should not
    fall over because an optional dependency is missing from a slim deployment.
    """
    if not rows:
        return None
    day = datetime.now(UTC)
    blob_service = clients.get_blob_service(settings)
    container = blob_service.get_container_client(settings.storage.silver_container)
    flat = [_flatten_row(row) for row in _dedupe_rows(rows)]

    try:
        import io

        import pyarrow as pa
        import pyarrow.parquet as pq

        buffer = io.BytesIO()
        pq.write_table(pa.Table.from_pylist(flat), buffer, compression="snappy")
        body, extension = buffer.getvalue(), "parquet"
    except ImportError:
        import csv
        import io

        logger.warning("pyarrow unavailable; writing the silver layer as CSV.")
        text = io.StringIO()
        writer = csv.DictWriter(text, fieldnames=sorted({k for row in flat for k in row}))
        writer.writeheader()
        writer.writerows(flat)
        body, extension = text.getvalue().encode("utf-8"), "csv"

    path = f"current/date={day:%Y-%m-%d}/current-{day:%Y%m%dT%H%M%S}.{extension}"
    container.upload_blob(name=path, data=body, overwrite=True)
    logger.info("Wrote silver table %s (%d rows).", path, len(flat))
    return path


def curate(hours: int = 24, settings: Settings | None = None) -> dict[str, Any]:
    """bronze -> silver (for Power BI) and serving (for the web dashboard)."""
    settings = settings or get_settings()
    if not settings.storage.enabled:
        raise RuntimeError("Curation requires STORAGE_ENABLED=true.")

    current_rows = read_bronze("current", hours=hours, settings=settings)
    breach_rows = read_bronze("threshold_breach", hours=hours, settings=settings)

    payloads = build_serving_payloads(current_rows, breach_rows)
    blob_service = clients.get_blob_service(settings)
    for path, payload in payloads.items():
        sinks.write_json_blob(
            blob_service, settings.storage.serving_container, path, payload
        )

    silver_path = _write_silver(current_rows, settings)
    summary = {
        "bronze_current_rows": len(current_rows),
        "bronze_breach_rows": len(breach_rows),
        "serving_files": list(payloads),
        "silver_path": silver_path,
    }
    logger.info("CURATE %s", summary, extra={"custom_dimensions": summary})
    return summary
