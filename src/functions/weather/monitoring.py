"""Threshold evaluation — the 'monitoring' half of the original requirement.

An ingestion pipeline that only stores data is a data pipeline; what made the
syslog project a *monitoring* project was deciding, at ingest time, which
records deserve attention. This module is that decision, kept as pure functions
so the rules can be unit tested without deploying anything.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from .config import MonitoringSettings
from .models import CurrentWeatherRecord, ThresholdBreach, _iso, make_record_id, utcnow

logger = logging.getLogger(__name__)

# Severity is coarse on purpose: two levels an on-call person can act on.
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"


def _breach(
    record: CurrentWeatherRecord,
    *,
    metric: str,
    value: float,
    threshold: float,
    comparison: str,
    severity: str,
    message: str,
    detected_at: str,
) -> ThresholdBreach:
    return ThresholdBreach(
        record_id=make_record_id("breach", record.record_id, metric),
        location_key=record.location_key,
        metric=metric,
        value=value,
        threshold=threshold,
        comparison=comparison,
        severity=severity,
        observed_at_utc=record.observed_at_utc,
        detected_at_utc=detected_at,
        message=message,
    )


def evaluate(
    record: CurrentWeatherRecord,
    settings: MonitoringSettings,
) -> list[ThresholdBreach]:
    """Return every threshold this reading crosses (possibly none)."""
    detected_at = _iso(utcnow()) or ""
    place = record.location.name or record.location_key
    breaches: list[ThresholdBreach] = []

    if record.temp_c is not None:
        if record.temp_c >= settings.max_temp_c:
            breaches.append(
                _breach(
                    record,
                    metric="temp_c",
                    value=record.temp_c,
                    threshold=settings.max_temp_c,
                    comparison=">=",
                    severity=SEVERITY_CRITICAL,
                    message=f"{place}: extreme heat, {record.temp_c}°C",
                    detected_at=detected_at,
                )
            )
        elif record.temp_c <= settings.min_temp_c:
            breaches.append(
                _breach(
                    record,
                    metric="temp_c",
                    value=record.temp_c,
                    threshold=settings.min_temp_c,
                    comparison="<=",
                    severity=SEVERITY_CRITICAL,
                    message=f"{place}: extreme cold, {record.temp_c}°C",
                    detected_at=detected_at,
                )
            )

    if record.wind_kph is not None and record.wind_kph >= settings.max_wind_kph:
        breaches.append(
            _breach(
                record,
                metric="wind_kph",
                value=record.wind_kph,
                threshold=settings.max_wind_kph,
                comparison=">=",
                severity=SEVERITY_WARNING,
                message=f"{place}: high wind, {record.wind_kph} km/h",
                detected_at=detected_at,
            )
        )

    pm2_5 = record.air_quality.pm2_5
    if pm2_5 is not None and pm2_5 >= settings.max_pm2_5:
        breaches.append(
            _breach(
                record,
                metric="pm2_5",
                value=pm2_5,
                threshold=settings.max_pm2_5,
                comparison=">=",
                severity=SEVERITY_WARNING,
                message=f"{place}: elevated PM2.5, {pm2_5} µg/m³",
                detected_at=detected_at,
            )
        )

    epa = record.air_quality.us_epa_index
    if epa is not None and epa >= settings.max_us_epa_index:
        breaches.append(
            _breach(
                record,
                metric="us_epa_index",
                value=float(epa),
                threshold=float(settings.max_us_epa_index),
                comparison=">=",
                severity=SEVERITY_CRITICAL if epa >= 5 else SEVERITY_WARNING,
                message=f"{place}: US EPA air quality index {epa}",
                detected_at=detected_at,
            )
        )

    return breaches


def log_breaches(breaches: Iterable[ThresholdBreach]) -> None:
    """Emit breaches as structured logs.

    Application Insights picks these up from stdout, which is what the Azure
    Monitor alert rules in infra/ query. Logging is the alerting transport —
    no extra service required.
    """
    for breach in breaches:
        level = logging.ERROR if breach.severity == SEVERITY_CRITICAL else logging.WARNING
        logger.log(
            level,
            "THRESHOLD_BREACH %s",
            breach.message,
            extra={"custom_dimensions": breach.to_dict()},
        )
