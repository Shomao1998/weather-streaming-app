"""Where per-session advice state lives.

Two implementations behind one protocol: in-memory for tests and local runs,
blob-backed for Azure. The service depends on the protocol, so neither the
policy nor the API knows which one is in play.

State is keyed by an anonymous, client-generated session id. Nothing else
about the caller is stored — no address, no user agent, no identifier that
outlives the browser session.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from ..config import Settings, get_settings
from .frequency import MuteRecord, SessionState, ShownRecord
from .models import parse_iso

logger = logging.getLogger(__name__)

STATE_PREFIX = "advice_state"
# State older than this is worthless: every window and mute has expired.
STATE_TTL_HOURS = 48
MAX_RECORDS_PER_SESSION = 50


class AdviceStateRepository(Protocol):
    def load(self, session_id: str) -> SessionState: ...

    def record_shown(self, session_id: str, record: ShownRecord) -> None: ...

    def record_mute(self, session_id: str, record: MuteRecord) -> None: ...


def _prune(state: SessionState, now: datetime) -> SessionState:
    cutoff = now - timedelta(hours=STATE_TTL_HOURS)
    shown = tuple(r for r in state.shown if r.shown_at > cutoff)[-MAX_RECORDS_PER_SESSION:]
    mutes = tuple(m for m in state.mutes if m.muted_until > now)
    return SessionState(shown=shown, mutes=mutes)


def _to_dict(state: SessionState) -> dict[str, Any]:
    return {
        "shown": [
            {
                "dedup_key": r.dedup_key,
                "trigger": r.trigger,
                "severity": r.severity,
                "shown_at": r.shown_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            }
            for r in state.shown
        ],
        "mutes": [
            {
                "trigger": m.trigger,
                "muted_until": m.muted_until.astimezone(UTC)
                .isoformat()
                .replace("+00:00", "Z"),
            }
            for m in state.mutes
        ],
    }


def _from_dict(payload: dict[str, Any]) -> SessionState:
    shown = []
    for row in payload.get("shown", []):
        when = parse_iso(row.get("shown_at"))
        if when:
            shown.append(
                ShownRecord(
                    dedup_key=str(row.get("dedup_key", "")),
                    trigger=str(row.get("trigger", "")),
                    severity=str(row.get("severity", "")),
                    shown_at=when,
                )
            )
    mutes = []
    for row in payload.get("mutes", []):
        until = parse_iso(row.get("muted_until"))
        if until:
            mutes.append(MuteRecord(trigger=str(row.get("trigger", "")), muted_until=until))
    return SessionState(shown=tuple(shown), mutes=tuple(mutes))


class InMemoryAdviceRepository:
    """Default for local runs and tests. Lost on restart, which is fine —
    losing frequency state shows a card again; it never shows a wrong one."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[str, SessionState] = {}

    def load(self, session_id: str) -> SessionState:
        with self._lock:
            return self._states.get(session_id, SessionState())

    def record_shown(self, session_id: str, record: ShownRecord) -> None:
        with self._lock:
            state = self._states.get(session_id, SessionState())
            self._states[session_id] = _prune(
                SessionState(shown=(*state.shown, record), mutes=state.mutes), record.shown_at
            )

    def record_mute(self, session_id: str, record: MuteRecord) -> None:
        with self._lock:
            state = self._states.get(session_id, SessionState())
            others = tuple(m for m in state.mutes if m.trigger != record.trigger)
            self._states[session_id] = SessionState(shown=state.shown, mutes=(*others, record))


class BlobAdviceRepository:
    """Blob-backed state, one small document per session.

    Reads are cached in-process for the life of a worker; writes go straight
    through. A failure to persist is logged and swallowed: the worst case is a
    card shown twice, which is a much better outcome than a 500 on a page the
    user came to for the weather.
    """

    def __init__(self, blob_service: Any, container: str) -> None:
        self._blob_service = blob_service
        self._container = container
        self._lock = threading.RLock()
        self._cache: dict[str, SessionState] = {}

    def _path(self, session_id: str) -> str:
        return f"{STATE_PREFIX}/{session_id}.json"

    def load(self, session_id: str) -> SessionState:
        with self._lock:
            if session_id in self._cache:
                return self._cache[session_id]
        state = SessionState()
        try:
            container = self._blob_service.get_container_client(self._container)
            raw = container.download_blob(self._path(session_id)).readall()
            state = _from_dict(json.loads(raw.decode("utf-8")))
        except Exception:
            # A missing blob is the normal first-visit case, not an error.
            logger.debug("No stored advice state for this session.")
        with self._lock:
            self._cache[session_id] = state
        return state

    def _save(self, session_id: str, state: SessionState) -> None:
        with self._lock:
            self._cache[session_id] = state
        try:
            container = self._blob_service.get_container_client(self._container)
            container.upload_blob(
                name=self._path(session_id),
                data=json.dumps(_to_dict(state)).encode("utf-8"),
                overwrite=True,
            )
        except Exception:
            logger.warning("Could not persist advice state; frequency control may reset.")

    def record_shown(self, session_id: str, record: ShownRecord) -> None:
        state = self.load(session_id)
        self._save(
            session_id,
            _prune(SessionState(shown=(*state.shown, record), mutes=state.mutes), record.shown_at),
        )

    def record_mute(self, session_id: str, record: MuteRecord) -> None:
        state = self.load(session_id)
        others = tuple(m for m in state.mutes if m.trigger != record.trigger)
        self._save(session_id, SessionState(shown=state.shown, mutes=(*others, record)))


_default: AdviceStateRepository | None = None
_default_lock = threading.RLock()


def get_repository(settings: Settings | None = None) -> AdviceStateRepository:
    """Blob-backed when storage is configured, in-memory otherwise."""
    global _default
    settings = settings or get_settings()
    with _default_lock:
        if _default is None:
            if settings.storage.enabled:
                from .. import clients

                _default = BlobAdviceRepository(
                    clients.get_blob_service(settings), settings.storage.serving_container
                )
            else:
                _default = InMemoryAdviceRepository()
        return _default


def reset_for_tests() -> None:
    global _default
    with _default_lock:
        _default = None
