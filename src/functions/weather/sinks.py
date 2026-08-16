"""Where records go: Event Hub (stream) and Blob Storage (lake).

Both implement the same tiny protocol, so a caller never knows which one it is
talking to, and local development can run with Event Hub switched off.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from .config import Settings, get_settings

logger = logging.getLogger(__name__)

# Event Hub caps a single event at 1 MB; our records are ~1 KB, so batching is
# about round-trips, not size. Kept explicit so the limit is visible.
MAX_EVENTS_PER_BATCH = 100

# Serialises sends across every EventHubSink. It is module-level on purpose:
# the producer it protects is a process-wide singleton (clients.get_event_hub_
# producer), but build_ingest_sink makes a fresh EventHubSink per call, so a
# per-instance lock would hand each concurrent invocation its own lock and
# guard nothing. The 30s and 30min timers collide every half hour; without a
# shared lock they would enter create_batch()/send_batch() on the same
# not-thread-safe producer at once.
_SEND_LOCK = threading.Lock()


def _to_payload(record: Any) -> dict[str, Any]:
    if hasattr(record, "to_dict"):
        return record.to_dict()
    if isinstance(record, dict):
        return record
    raise TypeError(f"Cannot serialise {type(record).__name__}")


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


class Sink(Protocol):
    name: str

    def emit(self, records: Sequence[Any]) -> int:
        """Write records. Returns how many were written."""


@dataclass
class EventHubSink:
    """Publishes records to Event Hub, batched."""

    producer: Any
    name: str = "eventhub"
    # Shared, not per-instance: see _SEND_LOCK. The SDK's producer is not
    # documented as thread-safe, and every sink over the singleton producer
    # must contend for the same lock or the serialisation is an illusion.
    _lock: threading.Lock = field(default=_SEND_LOCK, repr=False, compare=False)

    def emit(self, records: Sequence[Any]) -> int:
        if not records:
            return 0

        from azure.eventhub import EventData

        written = 0
        with self._lock:
            for start in range(0, len(records), MAX_EVENTS_PER_BATCH):
                chunk = records[start : start + MAX_EVENTS_PER_BATCH]
                batch = self.producer.create_batch()
                for record in chunk:
                    payload = _to_payload(record)
                    event = EventData(_dumps(payload))
                    # Properties are readable by consumers without parsing the
                    # body, which keeps routing and filtering cheap.
                    event.properties = {
                        "record_type": payload.get("record_type", "unknown"),
                        "location_key": payload.get("location_key", ""),
                        "schema_version": payload.get("schema_version", ""),
                    }
                    batch.add(event)
                self.producer.send_batch(batch)
                written += len(chunk)
        logger.info("Sent %d record(s) to Event Hub.", written)
        return written


def bronze_blob_path(record_type: str, when: datetime, suffix: str = "jsonl") -> str:
    """Hive-style partitioning: readable by Power BI, Spark, Fabric and DuckDB alike."""
    when = when.astimezone(UTC)
    return (
        f"{record_type}/"
        f"date={when:%Y-%m-%d}/"
        f"hour={when:%H}/"
        f"{when:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}.{suffix}"
    )


@dataclass
class BlobSink:
    """Appends records to the bronze layer as newline-delimited JSON."""

    blob_service: Any
    container: str
    name: str = "blob"

    def emit(self, records: Sequence[Any]) -> int:
        if not records:
            return 0
        payloads = [_to_payload(record) for record in records]
        record_type = payloads[0].get("record_type", "unknown")
        body = "\n".join(_dumps(p) for p in payloads).encode("utf-8")
        path = bronze_blob_path(record_type, datetime.now(UTC))

        container_client = self.blob_service.get_container_client(self.container)
        container_client.upload_blob(name=path, data=body, overwrite=False)
        logger.info("Wrote %d record(s) to %s/%s.", len(payloads), self.container, path)
        return len(payloads)


@dataclass
class CompositeSink:
    """Fans out to several sinks; one failing sink must not silence the others."""

    sinks: Sequence[Sink]
    name: str = "composite"

    def emit(self, records: Sequence[Any]) -> int:
        if not records:
            return 0
        written = 0
        errors: list[str] = []
        for sink in self.sinks:
            try:
                written += sink.emit(records)
            except Exception as exc:
                errors.append(f"{sink.name}: {exc}")
                logger.exception("Sink '%s' failed.", sink.name)
        if errors and written == 0:
            raise RuntimeError("All sinks failed -> " + "; ".join(errors))
        return written


def build_ingest_sink(settings: Settings | None = None) -> Sink:
    """Assemble the sink stack the ingest functions write to."""
    from . import clients

    settings = settings or get_settings()
    sinks: list[Sink] = []

    if settings.event_hub.enabled:
        sinks.append(EventHubSink(producer=clients.get_event_hub_producer(settings)))
    if settings.storage.enabled and not settings.event_hub.enabled:
        # With Event Hub on, the lake is fed by the event-hub-triggered
        # function instead, so ingest must not double-write.
        sinks.append(
            BlobSink(
                blob_service=clients.get_blob_service(settings),
                container=settings.storage.bronze_container,
            )
        )

    if not sinks:
        raise RuntimeError("No sink is enabled; check EVENT_HUB_ENABLED / STORAGE_ENABLED.")
    return sinks[0] if len(sinks) == 1 else CompositeSink(sinks=sinks)


def write_json_blob(
    blob_service: Any,
    container: str,
    path: str,
    payload: Any,
) -> None:
    """Overwrite a single JSON blob — used for the small serving-layer files."""
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    container_client = blob_service.get_container_client(container)
    container_client.upload_blob(
        name=path,
        data=body,
        overwrite=True,
        content_settings=_json_content_settings(),
    )
    logger.info("Updated %s/%s (%d bytes).", container, path, len(body))


def _json_content_settings() -> Any:
    from azure.storage.blob import ContentSettings

    # The dashboard fetches these directly from the browser; without the right
    # content type and a short cache window it either fails or serves stale data.
    return ContentSettings(content_type="application/json", cache_control="max-age=30")


def iter_jsonl(raw: bytes) -> Iterable[dict[str, Any]]:
    """Parse a bronze blob back into records, skipping corrupt lines."""
    for line in raw.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            logger.warning("Skipping malformed JSONL line.")
