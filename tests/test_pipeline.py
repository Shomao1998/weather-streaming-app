"""Pipeline orchestration and the serving-layer shape the dashboard consumes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from weather import clients, pipeline, sinks, transform
from weather.api import WeatherApiError
from weather.config import load_settings


@pytest.fixture
def local_settings(monkeypatch):
    monkeypatch.setenv("WEATHER_API_KEY", "local-test-key")
    monkeypatch.setenv("WEATHER_LOCATIONS", "Tokyo,Osaka")
    monkeypatch.setenv("EVENT_HUB_ENABLED", "false")
    monkeypatch.setenv("STORAGE_ENABLED", "true")
    monkeypatch.setenv("STORAGE_CONNECTION_STRING", "UseDevelopmentStorage=true")
    return load_settings()


class RecordingSink:
    name = "recording"

    def __init__(self):
        self.records = []

    def emit(self, records):
        self.records.extend(records)
        return len(records)


class StubClient:
    def __init__(self, current_payload=None, forecast_payload=None, failures=()):
        self._current = current_payload
        self._forecast = forecast_payload
        self._failures = set(failures)

    def current(self, location):
        if location in self._failures:
            raise WeatherApiError(f"{location} exploded")
        return self._current

    def forecast(self, location, days=3):
        if location in self._failures:
            raise WeatherApiError(f"{location} exploded")
        return self._forecast


def _patch(monkeypatch, client, sink):
    monkeypatch.setattr(clients, "get_weather_client", lambda settings=None: client)
    monkeypatch.setattr(sinks, "build_ingest_sink", lambda settings=None: sink)


class TestIngestCurrent:
    def test_collects_one_record_per_location(self, monkeypatch, local_settings, current_payload):
        sink = RecordingSink()
        _patch(monkeypatch, StubClient(current_payload=current_payload), sink)

        result = pipeline.ingest_current(local_settings)

        assert result.records_collected == 2
        assert result.succeeded
        assert len(sink.records) == 2

    def test_a_failing_location_does_not_stop_the_others(
        self, monkeypatch, local_settings, current_payload
    ):
        sink = RecordingSink()
        _patch(
            monkeypatch,
            StubClient(current_payload=current_payload, failures=["Osaka"]),
            sink,
        )

        result = pipeline.ingest_current(local_settings)

        assert result.records_collected == 1
        assert result.failed_locations == ["Osaka"]
        assert not result.succeeded  # surfaced so the alert rule can fire

    def test_breaches_are_emitted_alongside_readings(
        self, monkeypatch, local_settings, current_payload
    ):
        current_payload["current"]["temp_c"] = 45.0
        sink = RecordingSink()
        _patch(monkeypatch, StubClient(current_payload=current_payload), sink)

        result = pipeline.ingest_current(local_settings)

        assert result.breaches == 2  # one per location
        types = {r.record_type for r in sink.records}
        assert types == {"current", "threshold_breach"}

    def test_total_failure_writes_nothing(self, monkeypatch, local_settings, current_payload):
        sink = RecordingSink()
        _patch(
            monkeypatch,
            StubClient(current_payload=current_payload, failures=["Tokyo", "Osaka"]),
            sink,
        )

        result = pipeline.ingest_current(local_settings)

        assert result.records_written == 0
        assert sink.records == []


class TestIngestForecast:
    def test_produces_forecast_days_and_alerts(
        self, monkeypatch, local_settings, forecast_payload
    ):
        sink = RecordingSink()
        _patch(monkeypatch, StubClient(forecast_payload=forecast_payload), sink)

        result = pipeline.ingest_forecast(local_settings)

        # 3 forecast days + 1 alert, for each of 2 locations
        assert result.records_collected == 8
        assert sum(r.record_type == "alert" for r in sink.records) == 2


class TestServingPayloads:
    def _rows(self, current_payload, count=3):
        rows = []
        for index in range(count):
            current_payload["current"]["last_updated_epoch"] = 1754092500 + index * 900
            current_payload["current"]["temp_c"] = 30.0 + index
            rows.append(transform.to_current_record(current_payload).to_dict())
        return rows

    def test_latest_reports_the_most_recent_observation(self, current_payload):
        payloads = pipeline.build_serving_payloads(self._rows(current_payload), [])
        latest = payloads[pipeline.SERVING_LATEST_BLOB]

        assert len(latest["locations"]) == 1
        location = latest["locations"][0]
        assert location["name"] == "Tokyo"
        assert location["temp_c"] == 32.0  # the newest of the three
        assert location["observation_count_24h"] == 3

    def test_duplicate_polls_collapse_to_one_point(self, current_payload):
        rows = self._rows(current_payload, count=2)
        payloads = pipeline.build_serving_payloads(rows + rows + rows, [])
        series = payloads[pipeline.SERVING_TIMESERIES_BLOB]["series"][0]

        assert len(series["points"]) == 2

    def test_timeseries_is_sorted_oldest_first(self, current_payload):
        payloads = pipeline.build_serving_payloads(self._rows(current_payload), [])
        points = payloads[pipeline.SERVING_TIMESERIES_BLOB]["series"][0]["points"]

        assert [p["temp_c"] for p in points] == [30.0, 31.0, 32.0]

    def test_serving_payload_is_flat_for_the_browser(self, current_payload):
        payloads = pipeline.build_serving_payloads(self._rows(current_payload), [])
        location = payloads[pipeline.SERVING_LATEST_BLOB]["locations"][0]

        assert location["pm2_5"] == 18.6
        assert location["us_epa_index"] == 2

    def test_handles_an_empty_lake(self):
        payloads = pipeline.build_serving_payloads([], [])

        assert payloads[pipeline.SERVING_LATEST_BLOB]["locations"] == []
        assert payloads[pipeline.SERVING_TIMESERIES_BLOB]["series"] == []
        assert payloads[pipeline.SERVING_BREACHES_BLOB]["breaches"] == []

    def test_breaches_are_newest_first_and_capped(self, current_payload):
        base = datetime(2025, 8, 2, tzinfo=UTC)
        breaches = [
            {
                "record_id": f"b{i}",
                "detected_at_utc": (base + timedelta(minutes=i))
                .isoformat()
                .replace("+00:00", "Z"),
            }
            for i in range(120)
        ]
        payloads = pipeline.build_serving_payloads([], breaches)
        result = payloads[pipeline.SERVING_BREACHES_BLOB]["breaches"]

        assert len(result) == 100
        assert result[0]["record_id"] == "b119"
        assert result[-1]["record_id"] == "b20"


class TestNextHourRain:
    """The serving layer joins the hourly forecast onto each location.

    `build_serving_payloads` reads the wall clock to decide what "upcoming"
    means, so these fixtures are built relative to now rather than to a frozen
    date — otherwise every hour would already be in the past.
    """

    def _hourly(self, *offsets_and_chances):
        now = datetime.now(UTC)
        return [
            {
                "location_key": "35.6895,139.6917",
                "time_utc": (now + timedelta(hours=offset))
                .replace(minute=0, second=0, microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
                "chance_of_rain": chance,
            }
            for offset, chance in offsets_and_chances
        ]

    def test_picks_the_nearest_upcoming_hour(self, current_payload):
        rows = [transform.to_current_record(current_payload).to_dict()]
        payloads = pipeline.build_serving_payloads(
            rows, [], self._hourly((1, 85), (3, 10))
        )
        location = payloads[pipeline.SERVING_LATEST_BLOB]["locations"][0]
        assert location["precip_chance_next_hour"] == 85

    def test_ignores_hours_beyond_the_look_ahead(self, current_payload):
        rows = [transform.to_current_record(current_payload).to_dict()]
        payloads = pipeline.build_serving_payloads(rows, [], self._hourly((9, 95)))
        location = payloads[pipeline.SERVING_LATEST_BLOB]["locations"][0]
        # Tomorrow's rain must never answer "should I take an umbrella now".
        assert location["precip_chance_next_hour"] is None

    def test_ignores_hours_already_past(self, current_payload):
        rows = [transform.to_current_record(current_payload).to_dict()]
        payloads = pipeline.build_serving_payloads(rows, [], self._hourly((-3, 95)))
        assert (
            payloads[pipeline.SERVING_LATEST_BLOB]["locations"][0][
                "precip_chance_next_hour"
            ]
            is None
        )

    def test_absent_hourly_data_is_null_not_zero(self, current_payload):
        rows = [transform.to_current_record(current_payload).to_dict()]
        payloads = pipeline.build_serving_payloads(rows, [])
        location = payloads[pipeline.SERVING_LATEST_BLOB]["locations"][0]
        # Null means "unknown"; zero would mean "definitely dry", and the rule
        # treats those very differently.
        assert location["precip_chance_next_hour"] is None


class _FakeContainer:
    """Captures silver writes so a test can inspect blob names and overwrites.

    Mimics the one method _write_silver calls. `overwrite=True` replaces an
    existing name in place, which is exactly the property the idempotent-file
    fix relies on.
    """

    def __init__(self):
        self.blobs: dict[str, bytes] = {}
        self.write_count = 0

    def upload_blob(self, name, data, overwrite=False):
        if name in self.blobs and not overwrite:
            raise FileExistsError(name)
        self.blobs[name] = data
        self.write_count += 1


class _FakeBlobService:
    def __init__(self, container):
        self._container = container

    def get_container_client(self, _name):
        return self._container


class TestSilverIsIdempotent:
    """The bug this guards: curate runs hourly over a rolling 24h window, so a
    filename that varied per run left each observation duplicated across many
    files. A reader over `current/**` would then read it many times over."""

    def _record(self, current_payload, epoch, temp):
        current_payload["current"]["last_updated_epoch"] = epoch
        current_payload["current"]["temp_c"] = temp
        return transform.to_current_record(current_payload).to_dict()

    def test_one_file_per_observation_date_overwritten_in_place(
        self, monkeypatch, local_settings, current_payload
    ):
        container = _FakeContainer()
        monkeypatch.setattr(
            pipeline.clients, "get_blob_service",
            lambda settings=None: _FakeBlobService(container),
        )

        # Two observations on 2026-08-15, one on 2026-08-16 (UTC).
        d15 = int(datetime(2026, 8, 15, 6, 0, tzinfo=UTC).timestamp())
        d16 = int(datetime(2026, 8, 16, 6, 0, tzinfo=UTC).timestamp())
        rows = [
            self._record(current_payload, d15, 30.0),
            self._record(current_payload, d15 + 900, 31.0),
            self._record(current_payload, d16, 26.0),
        ]

        pipeline._write_silver(rows, local_settings)

        # Exactly one file per observation date — not per run, not per record.
        assert set(container.blobs) == {
            "current/date=2026-08-15/current.parquet",
            "current/date=2026-08-16/current.parquet",
        }

    def test_a_second_curate_overwrites_rather_than_accumulates(
        self, monkeypatch, local_settings, current_payload
    ):
        container = _FakeContainer()
        monkeypatch.setattr(
            pipeline.clients, "get_blob_service",
            lambda settings=None: _FakeBlobService(container),
        )
        epoch = int(datetime(2026, 8, 16, 6, 0, tzinfo=UTC).timestamp())
        rows = [self._record(current_payload, epoch, 26.0)]

        pipeline._write_silver(rows, local_settings)
        pipeline._write_silver(rows, local_settings)  # next hour, same window

        # Two curate runs, still one file for that day: the second overwrote
        # the first instead of adding current-<timestamp2>.parquet beside it.
        assert list(container.blobs) == ["current/date=2026-08-16/current.parquet"]
        assert container.write_count == 2  # both writes happened, to the same name

    def test_a_row_without_an_observation_time_is_dropped_not_misfiled(
        self, monkeypatch, local_settings, current_payload
    ):
        container = _FakeContainer()
        monkeypatch.setattr(
            pipeline.clients, "get_blob_service",
            lambda settings=None: _FakeBlobService(container),
        )
        good = self._record(
            current_payload, int(datetime(2026, 8, 16, tzinfo=UTC).timestamp()), 26.0
        )
        bad = {**good, "observed_at_utc": None}

        pipeline._write_silver([good, bad], local_settings)

        # A dateless row must not create a `current/date=/current.parquet`
        # bucket; it is left out rather than filed under an empty partition.
        assert list(container.blobs) == ["current/date=2026-08-16/current.parquet"]
