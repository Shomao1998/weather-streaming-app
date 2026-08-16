"""Sink behaviour: batching, partitioning and fan-out failure handling."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timezone

import pytest

from weather import sinks
from weather.models import CurrentWeatherRecord, Location


class FakeBatch:
    def __init__(self):
        self.events = []

    def add(self, event):
        self.events.append(event)


class FakeProducer:
    def __init__(self):
        self.batches = []

    def create_batch(self):
        return FakeBatch()

    def send_batch(self, batch):
        self.batches.append(batch)


class FakeContainerClient:
    def __init__(self):
        self.uploads = []

    def upload_blob(self, name, data, overwrite=False, content_settings=None):
        self.uploads.append(
            {"name": name, "data": data, "overwrite": overwrite, "cs": content_settings}
        )


class FakeBlobService:
    def __init__(self):
        self.containers = {}

    def get_container_client(self, container):
        return self.containers.setdefault(container, FakeContainerClient())


def _record(index: int = 0) -> CurrentWeatherRecord:
    return CurrentWeatherRecord(
        record_id=f"record-{index}",
        location_key="35.6895,139.6917",
        location=Location(name="Tokyo", country="Japan"),
        observed_at_utc="2025-08-01T23:55:00Z",
        ingested_at_utc="2025-08-02T00:00:00Z",
        temp_c=31.2 + index,
    )


class TestEventHubSink:
    def test_sends_one_batch_for_a_small_list(self):
        producer = FakeProducer()
        written = sinks.EventHubSink(producer=producer).emit([_record(0), _record(1)])

        assert written == 2
        assert len(producer.batches) == 1
        assert len(producer.batches[0].events) == 2

    def test_splits_into_batches_at_the_configured_size(self):
        producer = FakeProducer()
        records = [_record(i) for i in range(sinks.MAX_EVENTS_PER_BATCH + 5)]
        written = sinks.EventHubSink(producer=producer).emit(records)

        assert written == len(records)
        assert len(producer.batches) == 2
        assert len(producer.batches[1].events) == 5

    def test_sets_routing_properties_consumers_can_filter_on(self):
        producer = FakeProducer()
        sinks.EventHubSink(producer=producer).emit([_record()])
        event = producer.batches[0].events[0]

        assert event.properties["record_type"] == "current"
        assert event.properties["location_key"] == "35.6895,139.6917"
        assert event.properties["schema_version"] == "1.0"

    def test_body_is_valid_json(self):
        producer = FakeProducer()
        sinks.EventHubSink(producer=producer).emit([_record()])
        body = producer.batches[0].events[0].body_as_str(encoding="utf-8")

        assert json.loads(body)["temp_c"] == 31.2

    def test_empty_input_sends_nothing(self):
        producer = FakeProducer()
        assert sinks.EventHubSink(producer=producer).emit([]) == 0
        assert producer.batches == []

    def test_two_sinks_over_one_producer_share_a_lock(self):
        """The bug this guards: the producer is a process-wide singleton but
        build_ingest_sink makes a fresh sink per call. A per-instance lock would
        give each concurrent invocation its own lock and serialise nothing."""
        producer = FakeProducer()
        a = sinks.EventHubSink(producer=producer)
        b = sinks.EventHubSink(producer=producer)
        assert a._lock is b._lock is sinks._SEND_LOCK



class TestBlobSink:
    def test_writes_newline_delimited_json(self):
        service = FakeBlobService()
        written = sinks.BlobSink(blob_service=service, container="bronze").emit(
            [_record(0), _record(1)]
        )

        assert written == 2
        upload = service.containers["bronze"].uploads[0]
        lines = upload["data"].decode("utf-8").splitlines()
        assert len(lines) == 2
        assert json.loads(lines[1])["record_id"] == "record-1"

    def test_never_overwrites_a_bronze_blob(self):
        service = FakeBlobService()
        sinks.BlobSink(blob_service=service, container="bronze").emit([_record()])
        # Raw landing data is append-only; silently replacing a file would
        # destroy history.
        assert service.containers["bronze"].uploads[0]["overwrite"] is False


class TestBronzePath:
    def test_uses_hive_style_partitions(self):
        when = datetime(2025, 8, 2, 14, 30, 5, tzinfo=UTC)
        path = sinks.bronze_blob_path("current", when)

        assert path.startswith("current/date=2025-08-02/hour=14/")
        assert path.endswith(".jsonl")

    def test_converts_to_utc_before_partitioning(self):
        from datetime import timedelta

        tokyo = timezone(timedelta(hours=9))
        when = datetime(2025, 8, 2, 8, 0, tzinfo=tokyo)  # 2025-08-01 23:00 UTC
        assert sinks.bronze_blob_path("current", when).startswith(
            "current/date=2025-08-01/hour=23/"
        )

    def test_paths_are_unique_within_the_same_second(self):
        when = datetime(2025, 8, 2, 14, 30, 5, tzinfo=UTC)
        assert sinks.bronze_blob_path("current", when) != sinks.bronze_blob_path(
            "current", when
        )


class TestCompositeSink:
    def test_one_failing_sink_does_not_stop_the_others(self):
        class Failing:
            name = "failing"

            def emit(self, records):
                raise RuntimeError("boom")

        producer = FakeProducer()
        composite = sinks.CompositeSink(
            sinks=[Failing(), sinks.EventHubSink(producer=producer)]
        )

        assert composite.emit([_record()]) == 1
        assert len(producer.batches) == 1

    def test_raises_only_when_every_sink_fails(self):
        class Failing:
            name = "failing"

            def emit(self, records):
                raise RuntimeError("boom")

        composite = sinks.CompositeSink(sinks=[Failing(), Failing()])
        with pytest.raises(RuntimeError, match="All sinks failed"):
            composite.emit([_record()])


class TestJsonlParsing:
    def test_skips_corrupt_lines_instead_of_failing_the_batch(self):
        raw = b'{"a": 1}\nnot json\n\n{"b": 2}\n'
        assert list(sinks.iter_jsonl(raw)) == [{"a": 1}, {"b": 2}]
