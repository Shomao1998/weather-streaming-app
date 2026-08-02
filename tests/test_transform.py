"""Transform layer: the part that used to crash on any imperfect response."""

from __future__ import annotations

from datetime import UTC, datetime

from weather import transform
from weather.models import SCHEMA_VERSION

INGESTED_AT = datetime(2025, 8, 2, 0, 5, 0, tzinfo=UTC)


class TestCurrentRecord:
    def test_maps_the_fields_the_dashboard_depends_on(self, current_payload):
        record = transform.to_current_record(current_payload, ingested_at=INGESTED_AT)

        assert record.location.name == "Tokyo"
        assert record.location.country == "Japan"
        assert record.temp_c == 31.2
        assert record.feelslike_c == 36.4
        assert record.condition_text == "Partly cloudy"
        assert record.wind_kph == 13.0
        assert record.humidity == 68
        assert record.uv == 7.0
        assert record.schema_version == SCHEMA_VERSION
        assert record.record_type == "current"

    def test_uses_metric_units_only(self, current_payload):
        record = transform.to_current_record(current_payload)
        # The original mixed pressure_in / precip_in into an otherwise metric
        # record, which made the dashboard axes nonsense.
        assert record.pressure_mb == 1008.0
        assert record.precip_mm == 0.0

    def test_renames_hyphenated_air_quality_keys(self, current_payload):
        record = transform.to_current_record(current_payload)
        assert record.air_quality.us_epa_index == 2
        assert record.air_quality.gb_defra_index == 3
        assert record.air_quality.pm2_5 == 18.6

    def test_separates_observation_time_from_ingestion_time(self, current_payload):
        record = transform.to_current_record(current_payload, ingested_at=INGESTED_AT)
        assert record.observed_at_utc == "2025-08-01T23:55:00Z"
        assert record.ingested_at_utc == "2025-08-02T00:05:00Z"

    def test_record_id_is_stable_across_repeated_polls(self, current_payload):
        first = transform.to_current_record(current_payload, ingested_at=INGESTED_AT)
        later = transform.to_current_record(
            current_payload, ingested_at=datetime(2025, 8, 2, 0, 6, tzinfo=UTC)
        )
        # Same upstream observation polled twice -> one logical record.
        assert first.record_id == later.record_id

    def test_record_id_changes_when_the_observation_changes(self, current_payload):
        first = transform.to_current_record(current_payload)
        current_payload["current"]["last_updated_epoch"] = 1754093100
        second = transform.to_current_record(current_payload)
        assert first.record_id != second.record_id

    def test_record_id_differs_between_locations(self, current_payload):
        tokyo = transform.to_current_record(current_payload)
        current_payload["location"]["lat"] = 34.6937
        current_payload["location"]["lon"] = 135.5023
        osaka = transform.to_current_record(current_payload)
        assert tokyo.record_id != osaka.record_id

    def test_survives_a_completely_empty_payload(self):
        record = transform.to_current_record({})
        assert record.temp_c is None
        assert record.location.name is None
        assert record.air_quality.pm2_5 is None
        assert record.record_id  # still addressable

    def test_survives_nulls_and_wrong_types(self, current_payload):
        current_payload["current"]["temp_c"] = None
        current_payload["current"]["humidity"] = "not a number"
        current_payload["current"]["condition"] = "unexpectedly a string"
        record = transform.to_current_record(current_payload)
        assert record.temp_c is None
        assert record.humidity is None
        assert record.condition_text is None

    def test_flatten_produces_columns_not_nested_objects(self, current_payload):
        flat = transform.to_current_record(current_payload).flatten()
        assert flat["location_name"] == "Tokyo"
        assert flat["aqi_us_epa_index"] == 2
        assert "location" not in flat and "air_quality" not in flat


class TestForecastRecords:
    def test_one_record_per_day(self, forecast_payload):
        records = transform.to_forecast_records(forecast_payload)
        assert len(records) == 3
        assert [r.date for r in records] == ["2025-08-02", "2025-08-03", "2025-08-04"]

    def test_maps_day_level_metrics(self, forecast_payload):
        first = transform.to_forecast_records(forecast_payload)[0]
        assert first.maxtemp_c == 34.8
        assert first.mintemp_c == 26.1
        assert first.totalprecip_mm == 1.2
        assert first.daily_chance_of_rain == 63
        assert first.condition_text == "Patchy rain nearby"

    def test_returns_empty_list_when_no_forecast_present(self, current_payload):
        assert transform.to_forecast_records(current_payload) == []


class TestAlertRecords:
    def test_extracts_alerts_from_the_forecast_response(self, forecast_payload):
        alerts = transform.to_alert_records(forecast_payload)
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.event == "Heat Advisory"
        assert alert.severity == "Moderate"
        assert alert.instruction.startswith("Stay hydrated")

    def test_no_alerts_is_not_an_error(self, current_payload):
        assert transform.to_alert_records(current_payload) == []


class TestDeduplicate:
    def test_keeps_first_occurrence_and_order(self, current_payload):
        a = transform.to_current_record(current_payload)
        b = transform.to_current_record(current_payload)
        current_payload["current"]["last_updated_epoch"] = 1754099999
        c = transform.to_current_record(current_payload)

        unique = transform.deduplicate([a, b, c])
        assert [r.record_id for r in unique] == [a.record_id, c.record_id]
