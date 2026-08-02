"""Threshold rules — the monitoring behaviour this project is really about."""

from __future__ import annotations

from weather import monitoring, transform
from weather.config import MonitoringSettings

THRESHOLDS = MonitoringSettings()


def _record(current_payload, **overrides):
    current_payload["current"].update(overrides)
    return transform.to_current_record(current_payload)


def test_normal_conditions_produce_no_breach(current_payload):
    assert monitoring.evaluate(_record(current_payload), THRESHOLDS) == []


def test_extreme_heat_is_critical(current_payload):
    breaches = monitoring.evaluate(_record(current_payload, temp_c=39.5), THRESHOLDS)
    assert len(breaches) == 1
    assert breaches[0].metric == "temp_c"
    assert breaches[0].severity == monitoring.SEVERITY_CRITICAL
    assert "39.5" in breaches[0].message


def test_extreme_cold_is_critical(current_payload):
    breaches = monitoring.evaluate(_record(current_payload, temp_c=-15.0), THRESHOLDS)
    assert breaches[0].comparison == "<="
    assert breaches[0].severity == monitoring.SEVERITY_CRITICAL


def test_boundary_value_counts_as_a_breach(current_payload):
    breaches = monitoring.evaluate(_record(current_payload, temp_c=38.0), THRESHOLDS)
    assert len(breaches) == 1


def test_high_wind_is_a_warning(current_payload):
    breaches = monitoring.evaluate(_record(current_payload, wind_kph=75.0), THRESHOLDS)
    assert [b.metric for b in breaches] == ["wind_kph"]
    assert breaches[0].severity == monitoring.SEVERITY_WARNING


def test_air_quality_breaches_read_from_the_nested_block(current_payload):
    current_payload["current"]["air_quality"]["pm2_5"] = 90.0
    current_payload["current"]["air_quality"]["us-epa-index"] = 5
    breaches = monitoring.evaluate(transform.to_current_record(current_payload), THRESHOLDS)
    metrics = {b.metric for b in breaches}
    assert metrics == {"pm2_5", "us_epa_index"}
    assert next(b for b in breaches if b.metric == "us_epa_index").severity == (
        monitoring.SEVERITY_CRITICAL
    )


def test_multiple_simultaneous_breaches_are_all_reported(current_payload):
    record = _record(current_payload, temp_c=41.0, wind_kph=88.0)
    breaches = monitoring.evaluate(record, THRESHOLDS)
    assert {b.metric for b in breaches} == {"temp_c", "wind_kph"}


def test_missing_measurements_are_skipped_not_treated_as_zero(current_payload):
    current_payload["current"]["temp_c"] = None
    current_payload["current"]["wind_kph"] = None
    current_payload["current"]["air_quality"] = {}
    assert monitoring.evaluate(transform.to_current_record(current_payload), THRESHOLDS) == []


def test_breach_ids_are_deterministic_and_metric_specific(current_payload):
    record = _record(current_payload, temp_c=41.0, wind_kph=88.0)
    first = monitoring.evaluate(record, THRESHOLDS)
    second = monitoring.evaluate(record, THRESHOLDS)
    assert [b.record_id for b in first] == [b.record_id for b in second]
    assert len({b.record_id for b in first}) == 2


def test_custom_thresholds_are_respected(current_payload):
    strict = MonitoringSettings(max_temp_c=25.0)
    breaches = monitoring.evaluate(transform.to_current_record(current_payload), strict)
    assert [b.metric for b in breaches] == ["temp_c"]
