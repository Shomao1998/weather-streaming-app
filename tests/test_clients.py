"""Client caching and, above all, that the accessors do not deadlock.

The first deployment of this rewrite hung for five minutes on every
invocation and was killed by the host timeout, with no error logged. The cause
was a non-reentrant lock: ``get_weather_client`` held it while resolving the
API key, and ``get_secret`` took it again. Every test below that finishes
rather than hanging is asserting that the nesting still works.
"""

from __future__ import annotations

import threading

import pytest

from weather import clients
from weather.config import load_settings

# A deadlock makes a test hang forever rather than fail, so each one runs on a
# worker thread and the assertion is that the thread finished.
DEADLOCK_TIMEOUT_SECONDS = 10


def run_without_deadlock(fn):
    """Run fn on a thread; fail if it does not return promptly."""
    box: dict = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the main thread
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(DEADLOCK_TIMEOUT_SECONDS)

    assert not thread.is_alive(), (
        f"{getattr(fn, '__name__', fn)} did not return within "
        f"{DEADLOCK_TIMEOUT_SECONDS}s — the client accessors are deadlocking again."
    )
    if "error" in box:
        raise box["error"]
    return box["value"]


@pytest.fixture
def keyvault_settings(monkeypatch):
    """Force the Key Vault path — the one the local override short-circuits."""
    monkeypatch.delenv("WEATHER_API_KEY", raising=False)
    monkeypatch.setenv("KEY_VAULT_URL", "https://kv-example.vault.azure.net")
    monkeypatch.setenv("EVENT_HUB_ENABLED", "false")
    monkeypatch.setenv("STORAGE_ENABLED", "true")
    monkeypatch.setenv("STORAGE_CONNECTION_STRING", "UseDevelopmentStorage=true")
    clients.reset_for_tests()
    yield load_settings()
    clients.reset_for_tests()


@pytest.fixture
def fake_secret(monkeypatch):
    calls = {"n": 0}

    def _get_secret(vault_url, secret_name):
        calls["n"] += 1
        # Take the module lock exactly the way the real implementation does.
        with clients._lock:
            return "secret-from-vault"

    monkeypatch.setattr(clients, "get_secret", _get_secret)
    return calls


def test_lock_is_reentrant():
    assert isinstance(clients._lock, type(threading.RLock()))


def test_get_weather_client_does_not_deadlock(keyvault_settings, fake_secret):
    client = run_without_deadlock(lambda: clients.get_weather_client(keyvault_settings))
    assert client is not None
    assert fake_secret["n"] == 1


def test_weather_client_is_cached(keyvault_settings, fake_secret):
    first = run_without_deadlock(lambda: clients.get_weather_client(keyvault_settings))
    second = run_without_deadlock(lambda: clients.get_weather_client(keyvault_settings))

    assert first is second
    # One Key Vault round trip per worker, not one per invocation.
    assert fake_secret["n"] == 1


def test_local_override_skips_key_vault(monkeypatch):
    monkeypatch.setenv("WEATHER_API_KEY", "local-key")
    monkeypatch.setenv("EVENT_HUB_ENABLED", "false")
    monkeypatch.setenv("STORAGE_CONNECTION_STRING", "UseDevelopmentStorage=true")
    clients.reset_for_tests()

    def explode(*args, **kwargs):
        raise AssertionError("Key Vault must not be called when an override is set")

    monkeypatch.setattr(clients, "get_secret", explode)
    assert clients.resolve_weather_api_key(load_settings()) == "local-key"


def test_blob_service_accessor_does_not_deadlock(keyvault_settings, monkeypatch):
    """get_blob_service takes the lock, then get_credential takes it again."""
    sentinel = object()
    monkeypatch.setattr(clients, "get_credential", lambda: _locking_credential())

    class FakeBlobServiceClient:
        @staticmethod
        def from_connection_string(conn):
            return sentinel

    monkeypatch.setitem(
        __import__("sys").modules,
        "azure.storage.blob",
        type("m", (), {"BlobServiceClient": FakeBlobServiceClient}),
    )

    service = run_without_deadlock(lambda: clients.get_blob_service(keyvault_settings))
    assert service is sentinel


def _locking_credential():
    with clients._lock:
        return object()


def test_shutdown_closes_and_clears(monkeypatch):
    closed = {"producer": False, "weather": False}

    class FakeProducer:
        def close(self):
            closed["producer"] = True

    class FakeWeather:
        def close(self):
            closed["weather"] = True

    clients._event_hub_producer = FakeProducer()
    clients._weather_client = FakeWeather()

    run_without_deadlock(clients.shutdown)

    assert closed == {"producer": True, "weather": True}
    assert clients._event_hub_producer is None
    assert clients._weather_client is None


def test_shutdown_survives_a_producer_that_fails_to_close():
    class BadProducer:
        def close(self):
            raise RuntimeError("connection already gone")

    clients._event_hub_producer = BadProducer()
    run_without_deadlock(clients.shutdown)
    assert clients._event_hub_producer is None
