"""Deduplication, frequency control and expiry — all the "should we say this
again" logic, kept away from both the rules and the wording.

Pure functions over a state snapshot, so every policy question can be answered
in a unit test without a clock or a storage account.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..config import AdviceSettings
from .models import SEVERITY_ORDER, AdviceOutcome, AdviceTrigger, Severity


def dedup_key(
    location_key: str,
    trigger: AdviceTrigger | str,
    snapshot_id: str,
    rule_version: str,
) -> str:
    """Stable identity of "this advice, about this observation".

    Includes the rule version so that changing a threshold or the copy is
    allowed to reach users who already saw the previous card — without it, a
    fix would be invisible to exactly the people who saw the bug.
    """
    raw = f"{location_key}|{trigger}|{snapshot_id}|{rule_version}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class ShownRecord:
    """What the store remembers about a card already delivered."""

    dedup_key: str
    trigger: str
    severity: str
    shown_at: datetime


@dataclass(frozen=True)
class MuteRecord:
    trigger: str
    muted_until: datetime


@dataclass(frozen=True)
class SessionState:
    """Everything policy needs to know about one anonymous session."""

    shown: tuple[ShownRecord, ...] = ()
    mutes: tuple[MuteRecord, ...] = ()

    def last_shown(self, trigger: str) -> ShownRecord | None:
        matching = [r for r in self.shown if r.trigger == trigger]
        return max(matching, key=lambda r: r.shown_at) if matching else None

    def muted_until(self, trigger: str, now: datetime) -> datetime | None:
        active = [m for m in self.mutes if m.trigger == trigger and m.muted_until > now]
        return max((m.muted_until for m in active), default=None)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    outcome: AdviceOutcome
    reason: str = ""


def mute_until(now: datetime, settings: AdviceSettings) -> datetime:
    """When a mute expires.

    "Not again today" is what the button says, so the default honours the
    wording literally: the rest of the UTC day, not a rolling 24 hours.
    """
    if settings.mute_rest_of_day:
        return (now.astimezone(UTC) + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    return now + timedelta(minutes=settings.min_interval_minutes)


def decide(
    *,
    key: str,
    trigger: AdviceTrigger | str,
    severity: Severity | str,
    state: SessionState,
    now: datetime,
    settings: AdviceSettings,
) -> Decision:
    """Whether a freshly built card may be shown to this session."""
    trigger_code = str(trigger)
    previous = state.last_shown(trigger_code)
    escalated = previous is not None and (
        SEVERITY_ORDER.get(str(severity), 0) > SEVERITY_ORDER.get(previous.severity, 0)
    )

    muted = state.muted_until(trigger_code, now)
    if muted is not None and not escalated:
        return Decision(False, AdviceOutcome.SUPPRESSED_MUTED, f"muted until {muted.isoformat()}")

    if previous is None:
        return Decision(True, AdviceOutcome.GENERATED)

    # The same advice about the same observation is the same card. Showing it
    # twice is the single most annoying thing this feature could do, so it is
    # checked before the time window.
    if previous.dedup_key == key and not escalated:
        return Decision(False, AdviceOutcome.SUPPRESSED_FREQUENCY, "same snapshot already shown")

    elapsed = (now - previous.shown_at).total_seconds() / 60
    if elapsed < settings.min_interval_minutes and not escalated:
        return Decision(
            False,
            AdviceOutcome.SUPPRESSED_FREQUENCY,
            f"shown {elapsed:.0f}min ago, window is {settings.min_interval_minutes}min",
        )

    return Decision(True, AdviceOutcome.GENERATED)


def expires_at(now: datetime, settings: AdviceSettings) -> datetime:
    """A card describes a moment, so it has to stop being shown eventually."""
    return now + timedelta(minutes=settings.card_ttl_minutes)
