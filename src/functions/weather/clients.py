"""Lazily-created, process-wide Azure clients.

The original code built a credential, a Key Vault client and an Event Hub
producer on *every* 30-second tick and never closed any of them: a Key Vault
round-trip per invocation plus a leaked AMQP connection per invocation. Function
workers survive across invocations, so these belong at module scope, created
once and reused.
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
import time
from typing import Any

from .api import WeatherApiClient
from .config import Settings, get_settings

logger = logging.getLogger(__name__)

# Key Vault values change rarely; an hour of staleness is a fine price for
# removing 2,880 Key Vault calls a day.
SECRET_CACHE_TTL_SECONDS = 3600

# Reentrant on purpose: the accessors below nest — get_weather_client holds the
# lock while resolving the API key, which takes it again via get_secret, and
# both producer factories take it again via get_credential. A plain Lock
# deadlocks there, and the symptom is not an error but a function that hangs
# until the host's timeout kills it.
_lock = threading.RLock()
_credential: Any = None
_secret_cache: dict[str, tuple[str, float]] = {}
_event_hub_producer: Any = None
_blob_service: Any = None
_weather_client: WeatherApiClient | None = None


def get_credential() -> Any:
    """Managed identity on Azure, developer credentials locally.

    ``DefaultAzureCredential`` walks a chain of sources — environment, workload
    identity, IMDS, shared token cache, the Azure CLI, PowerShell — and several
    of those probes block rather than fail fast inside a Function App. That
    turns a missing-permission problem into a five-minute function timeout with
    no log line to explain it.

    On Azure the identity is known exactly (``AZURE_CLIENT_ID`` is set by the
    template), so ask for it directly and skip the chain.
    """
    global _credential
    if _credential is None:
        with _lock:
            if _credential is None:
                client_id = os.environ.get("AZURE_CLIENT_ID", "").strip()
                if client_id:
                    from azure.identity import ManagedIdentityCredential

                    logger.info("Using managed identity %s.", client_id)
                    _credential = ManagedIdentityCredential(client_id=client_id)
                else:
                    from azure.identity import DefaultAzureCredential

                    logger.info("AZURE_CLIENT_ID unset; falling back to DefaultAzureCredential.")
                    _credential = DefaultAzureCredential()
    return _credential


def get_secret(vault_url: str, secret_name: str) -> str:
    """Fetch a Key Vault secret, cached in-process with a TTL."""
    cache_key = f"{vault_url}/{secret_name}"
    cached = _secret_cache.get(cache_key)
    now = time.monotonic()
    if cached and now < cached[1]:
        return cached[0]

    from azure.keyvault.secrets import SecretClient

    client = SecretClient(vault_url=vault_url, credential=get_credential())
    value = client.get_secret(secret_name).value or ""
    if not value:
        raise RuntimeError(f"Key Vault secret '{secret_name}' is empty.")

    with _lock:
        _secret_cache[cache_key] = (value, now + SECRET_CACHE_TTL_SECONDS)
    logger.info("Refreshed secret '%s' from Key Vault.", secret_name)
    return value


def resolve_weather_api_key(settings: Settings | None = None) -> str:
    """Local override first (fast dev loop), Key Vault otherwise (production)."""
    settings = settings or get_settings()
    if settings.weather.api_key_override:
        return settings.weather.api_key_override
    return get_secret(
        settings.weather.key_vault_url, settings.weather.api_key_secret_name
    )


def get_weather_client(settings: Settings | None = None) -> WeatherApiClient:
    global _weather_client
    settings = settings or get_settings()
    if _weather_client is None:
        with _lock:
            if _weather_client is None:
                _weather_client = WeatherApiClient(
                    resolve_weather_api_key(settings),
                    base_url=settings.weather.base_url,
                    timeout_seconds=settings.weather.timeout_seconds,
                    max_retries=settings.weather.max_retries,
                )
    return _weather_client


def get_event_hub_producer(settings: Settings | None = None) -> Any:
    global _event_hub_producer
    settings = settings or get_settings()
    if _event_hub_producer is None:
        with _lock:
            if _event_hub_producer is None:
                from azure.eventhub import EventHubProducerClient, TransportType

                _event_hub_producer = EventHubProducerClient(
                    fully_qualified_namespace=settings.event_hub.namespace,
                    eventhub_name=settings.event_hub.name,
                    credential=get_credential(),
                    # AMQP's native port 5671 is not reliably open from a
                    # Function App; over websockets everything rides 443,
                    # which is the difference between working and hanging
                    # until the invocation times out.
                    transport_type=TransportType.AmqpOverWebsocket,
                    retry_total=3,
                )
                logger.info(
                    "Event Hub producer created for %s/%s.",
                    settings.event_hub.namespace,
                    settings.event_hub.name,
                )
    return _event_hub_producer


def get_blob_service(settings: Settings | None = None) -> Any:
    global _blob_service
    settings = settings or get_settings()
    if _blob_service is None:
        with _lock:
            if _blob_service is None:
                from azure.storage.blob import BlobServiceClient

                if settings.storage.connection_string:
                    # Local development against Azurite.
                    _blob_service = BlobServiceClient.from_connection_string(
                        settings.storage.connection_string
                    )
                else:
                    _blob_service = BlobServiceClient(
                        account_url=settings.storage.account_url,
                        credential=get_credential(),
                    )
    return _blob_service


def shutdown() -> None:
    """Close pooled connections when the worker goes away."""
    global _event_hub_producer, _weather_client
    with _lock:
        if _event_hub_producer is not None:
            try:
                _event_hub_producer.close()
            except Exception:  # pragma: no cover - best effort on teardown
                logger.warning("Event Hub producer did not close cleanly.", exc_info=True)
            _event_hub_producer = None
        if _weather_client is not None:
            _weather_client.close()
            _weather_client = None


def reset_for_tests() -> None:
    """Drop every cached client. Used by the test suite, never in production."""
    global _credential, _event_hub_producer, _blob_service, _weather_client
    with _lock:
        _credential = None
        _event_hub_producer = None
        _blob_service = None
        _weather_client = None
        _secret_cache.clear()


atexit.register(shutdown)
