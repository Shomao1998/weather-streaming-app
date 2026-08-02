"""Read side of the serving layer.

The dashboard is a static page with no backend of its own, and the lake is not
publicly readable — enabling anonymous blob access would expose the raw bronze
data alongside the curated files. Instead the Function App hands out the three
small serving documents over HTTP, which keeps the storage account private and
puts a cache in front of it.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from . import clients
from .config import Settings, get_settings

logger = logging.getLogger(__name__)

# The curation job rewrites these hourly, so anything under a minute of
# staleness is free. This exists to stop a refresh loop in an open browser tab
# from turning into one storage transaction per second.
CACHE_TTL_SECONDS = 30

_lock = threading.Lock()
_cache: dict[str, tuple[dict[str, Any], float]] = {}


class ServingDataUnavailable(RuntimeError):
    """The requested document has not been produced yet."""


def read_serving_document(blob_name: str, settings: Settings | None = None) -> dict[str, Any]:
    """Fetch one serving document, cached in-process."""
    settings = settings or get_settings()
    cached = _cache.get(blob_name)
    now = time.monotonic()
    if cached and now < cached[1]:
        return cached[0]

    blob_service = clients.get_blob_service(settings)
    container = blob_service.get_container_client(settings.storage.serving_container)
    try:
        raw = container.download_blob(blob_name).readall()
    except Exception as exc:
        # Before the first curation run the blobs genuinely do not exist; that
        # is a 503-with-an-explanation, not a 500.
        raise ServingDataUnavailable(
            f"'{blob_name}' has not been generated yet — the curate function "
            "produces it on its schedule."
        ) from exc

    payload = json.loads(raw.decode("utf-8"))
    with _lock:
        _cache[blob_name] = (payload, now + CACHE_TTL_SECONDS)
    return payload


def clear_cache() -> None:
    with _lock:
        _cache.clear()
