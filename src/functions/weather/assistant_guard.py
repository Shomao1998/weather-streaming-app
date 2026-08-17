"""The cost guard for the paid chat path.

Two limits, both enforced *before* a paid model call, so the ceiling does not
depend on Azure's billing latency:

* a **global daily budget** — the day's estimated spend, accrued in a blob so
  it is shared across workers. Once it crosses the cap, every caller gets the
  free deterministic answer for the rest of the day.
* a **per-session throttle** — an in-process sliding window, so one caller
  cannot drain the day's budget in minutes. Per-worker rather than global,
  which is enough because the budget above is the real ceiling.

Everything fails **closed**: if the guard cannot confirm there is budget left —
storage down, a parse error, anything — it denies the paid call and the
assistant serves the free answer. A paid call is a privilege the guard must
positively grant, never a default it forgets to withhold.

This module holds no model logic. It is the gate the chat path asks before
spending, and the meter it reports spend back to afterwards.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

GUARD_PREFIX = "_guard"


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    reason: str = ""


class BlobSpendStore:
    """Today's accrued spend, in one blob per UTC day, updated with optimistic
    concurrency so two workers do not lose each other's increments."""

    def __init__(self, blob_service: Any, container: str, clock=time.time) -> None:
        self._blob_service = blob_service
        self._container = container
        self._clock = clock

    def _path(self) -> str:
        day = datetime.fromtimestamp(self._clock(), UTC).strftime("%Y-%m-%d")
        return f"{GUARD_PREFIX}/spend-{day}.json"

    def _client(self) -> Any:
        return self._blob_service.get_blob_client(self._container, self._path())

    def read_spend(self) -> float:
        """Today's spend so far. Raises on a real storage error so the caller
        can fail closed; a missing blob is simply zero."""
        import json

        from azure.core.exceptions import ResourceNotFoundError

        try:
            data = self._client().download_blob().readall()
        except ResourceNotFoundError:
            return 0.0
        return float(json.loads(data).get("cost_usd", 0.0))

    def add_spend(self, cost_usd: float, attempts: int = 3) -> None:
        """Accrue spend into today's blob. Best-effort: a lost update under
        heavy contention slightly *under*-counts, which the huge headroom
        between real traffic and the cap makes irrelevant."""
        import json

        from azure.core import MatchConditions
        from azure.core.exceptions import (
            ResourceExistsError,
            ResourceModifiedError,
            ResourceNotFoundError,
        )

        # A write can lose the race two ways: the blob we created-from-nothing
        # now exists (ResourceExistsError), or the etag we held is stale
        # (ResourceModifiedError). Both mean "re-read and retry".
        conflicts = (ResourceExistsError, ResourceModifiedError)
        client = self._client()
        for _ in range(attempts):
            try:
                downloaded = client.download_blob()
                current = json.loads(downloaded.readall())
                etag = downloaded.properties.etag
            except ResourceNotFoundError:
                current, etag = {"cost_usd": 0.0, "calls": 0}, None

            current["cost_usd"] = round(float(current.get("cost_usd", 0.0)) + cost_usd, 6)
            current["calls"] = int(current.get("calls", 0)) + 1
            body = json.dumps(current).encode("utf-8")

            try:
                if etag is None:
                    client.upload_blob(body, overwrite=False)
                else:
                    client.upload_blob(
                        body, overwrite=True, etag=etag,
                        match_condition=MatchConditions.IfNotModified,
                    )
                return
            except conflicts:
                continue  # someone else wrote first; re-read and retry
        logger.warning("Cost guard could not record spend after %d attempts.", attempts)


class AssistantGuard:
    def __init__(self, settings: Any, store: Any | None = None, clock=time.time) -> None:
        rag = settings.rag
        self._daily_budget = float(rag.daily_budget_usd)
        self._session_max = int(rag.session_max_calls)
        self._session_window = int(rag.session_window_seconds)
        self._clock = clock
        self._store = store
        self._lock = threading.RLock()
        self._session_hits: dict[str, deque[float]] = defaultdict(deque)

    def _make_store(self, settings: Any) -> Any:
        from . import clients

        return BlobSpendStore(
            clients.get_blob_service(settings),
            settings.storage.bronze_container,
            clock=self._clock,
        )

    def check(self, session_id: str, settings: Any) -> GuardDecision:
        """May the assistant make one paid model call now?"""
        if self._daily_budget <= 0:
            return GuardDecision(False, "budget disabled")

        # 1) Per-session throttle — cheap, in-process, no I/O.
        now = self._clock()
        with self._lock:
            hits = self._session_hits[session_id]
            while hits and hits[0] < now - self._session_window:
                hits.popleft()
            if len(hits) >= self._session_max:
                return GuardDecision(False, "session rate limit")

        # 2) Global daily budget — the real ceiling. Fail closed on any error.
        try:
            store = self._store or self._make_store(settings)
            spent = store.read_spend()
        except Exception:
            logger.exception("Cost guard could not read the budget; denying to be safe.")
            return GuardDecision(False, "budget unavailable")

        if spent >= self._daily_budget:
            return GuardDecision(False, "daily budget reached")
        return GuardDecision(True)

    def record(self, session_id: str, cost_usd: float, settings: Any) -> None:
        """Report the cost of a call that was made, and count it against the
        session. Called only after a real paid call."""
        now = self._clock()
        with self._lock:
            self._session_hits[session_id].append(now)
        try:
            store = self._store or self._make_store(settings)
            store.add_spend(cost_usd)
        except Exception:
            logger.exception("Cost guard could not record spend.")


_guard: AssistantGuard | None = None
_guard_lock = threading.RLock()


def get_guard(settings: Any) -> AssistantGuard:
    """Process-wide guard, so the per-session window survives across requests."""
    global _guard
    with _guard_lock:
        if _guard is None:
            _guard = AssistantGuard(settings)
        return _guard


def reset_guard() -> None:
    """Drop the cached guard. Used by tests and after a config change."""
    global _guard
    with _guard_lock:
        _guard = None
