"""Serving layer reads: caching, and the difference between 'not yet' and 'broken'."""

from __future__ import annotations

import json

import pytest

from weather import clients, serving
from weather.config import load_settings


@pytest.fixture
def local_settings(monkeypatch):
    monkeypatch.setenv("WEATHER_API_KEY", "local-test-key")
    monkeypatch.setenv("EVENT_HUB_ENABLED", "false")
    monkeypatch.setenv("STORAGE_ENABLED", "true")
    monkeypatch.setenv("STORAGE_CONNECTION_STRING", "UseDevelopmentStorage=true")
    serving.clear_cache()
    yield load_settings()
    serving.clear_cache()


class FakeDownload:
    def __init__(self, payload):
        self._payload = payload

    def readall(self):
        return json.dumps(self._payload).encode("utf-8")


class FakeContainer:
    def __init__(self, documents):
        self.documents = documents
        self.downloads = 0

    def download_blob(self, name):
        self.downloads += 1
        if name not in self.documents:
            raise RuntimeError("BlobNotFound")
        return FakeDownload(self.documents[name])


class FakeBlobService:
    def __init__(self, documents):
        self.container = FakeContainer(documents)

    def get_container_client(self, name):
        return self.container


def _patch_blob(monkeypatch, documents):
    service = FakeBlobService(documents)
    monkeypatch.setattr(clients, "get_blob_service", lambda settings=None: service)
    return service


def test_reads_a_document(monkeypatch, local_settings):
    _patch_blob(monkeypatch, {"latest.json": {"locations": [{"name": "Tokyo"}]}})
    payload = serving.read_serving_document("latest.json", local_settings)
    assert payload["locations"][0]["name"] == "Tokyo"


def test_second_read_is_served_from_cache(monkeypatch, local_settings):
    service = _patch_blob(monkeypatch, {"latest.json": {"a": 1}})

    serving.read_serving_document("latest.json", local_settings)
    serving.read_serving_document("latest.json", local_settings)

    # An open browser tab polling every 30s must not become one storage
    # transaction per poll per viewer.
    assert service.container.downloads == 1


def test_cache_is_per_document(monkeypatch, local_settings):
    service = _patch_blob(monkeypatch, {"latest.json": {"a": 1}, "breaches_24h.json": {"b": 2}})

    serving.read_serving_document("latest.json", local_settings)
    serving.read_serving_document("breaches_24h.json", local_settings)

    assert service.container.downloads == 2


def test_expired_cache_refetches(monkeypatch, local_settings):
    service = _patch_blob(monkeypatch, {"latest.json": {"a": 1}})
    serving.read_serving_document("latest.json", local_settings)

    monkeypatch.setattr(serving, "CACHE_TTL_SECONDS", -1)
    serving.clear_cache()
    serving.read_serving_document("latest.json", local_settings)

    assert service.container.downloads == 2


def test_missing_document_is_a_typed_unavailable_error(monkeypatch, local_settings):
    _patch_blob(monkeypatch, {})
    with pytest.raises(serving.ServingDataUnavailable, match="has not been generated yet"):
        serving.read_serving_document("latest.json", local_settings)
