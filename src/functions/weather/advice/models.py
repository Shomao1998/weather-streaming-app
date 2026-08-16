"""Types for the advice card domain.

Every field the card protocol promises is declared here, so the API layer never
assembles a dict by hand and the shape cannot drift between the service, the
tests and the dashboard.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class AdviceTrigger(StrEnum):
    """Why a card was produced. Wire values are stable; do not rename."""

    RAIN_EXPECTED = "RAIN_EXPECTED"
    HIGH_UV = "HIGH_UV"
    EXTREME_HEAT = "EXTREME_HEAT"
    HIGH_WIND = "HIGH_WIND"
    NO_RECOMMENDATION = "NO_RECOMMENDATION"


class Severity(StrEnum):
    """How loudly a card should present itself.

    Ordered, because escalation is allowed to break through frequency control:
    a card the user already dismissed at `info` may return at `warning`.
    Official severe-weather warnings will slot in above these later, which is
    why severity is a scale rather than a boolean.
    """

    INFO = "info"
    WARNING = "warning"
    SEVERE = "severe"


SEVERITY_ORDER: dict[str, int] = {
    Severity.INFO: 1,
    Severity.WARNING: 2,
    Severity.SEVERE: 3,
}


class FeedbackEvent(StrEnum):
    SHOWN = "shown"
    CLICKED = "clicked"
    DISMISSED = "dismissed"
    MUTED = "muted"
    HELPFUL = "helpful"
    NOT_HELPFUL = "not_helpful"


class AdviceOutcome(StrEnum):
    """What an evaluation decided — the vocabulary of the structured logs."""

    GENERATED = "generated"
    NO_RULE_MATCHED = "no_rule_matched"
    STALE_WEATHER = "stale_weather"
    SUPPRESSED_FREQUENCY = "suppressed_frequency"
    SUPPRESSED_MUTED = "suppressed_muted"
    DISABLED = "disabled"
    UNKNOWN_LOCATION = "unknown_location"
    PROVIDER_FAILURE = "provider_failure"


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Evidence:
    """The number the advice is based on, shown to the user verbatim.

    A card that says "take an umbrella" without saying why reads as a guess.
    """

    label: str
    value: str


@dataclass(frozen=True)
class Action:
    type: str
    label: str


@dataclass(frozen=True)
class WeatherContext:
    """The weather facts a rule is allowed to see.

    Built from one entry of the serving snapshot, so rules never touch storage,
    HTTP or the serving document's shape.
    """

    location: str
    location_key: str
    observed_at_utc: datetime | None
    temp_c: float | None = None
    feelslike_c: float | None = None
    uv: float | None = None
    wind_kph: float | None = None
    precip_chance_next_hour: int | None = None
    precip_forecast_hour_utc: str | None = None
    condition_text: str | None = None

    @property
    def snapshot_id(self) -> str:
        """Identity of the observation, not of this request.

        Two requests against the same observation must produce the same id, or
        deduplication cannot work.
        """
        raw = f"{self.location_key}|{_iso(self.observed_at_utc) if self.observed_at_utc else ''}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def age(self, now: datetime) -> float | None:
        """Minutes since the observation, or None when it has no timestamp."""
        if self.observed_at_utc is None:
            return None
        return (now - self.observed_at_utc).total_seconds() / 60

    @classmethod
    def from_serving_location(cls, entry: dict[str, Any]) -> WeatherContext:
        return cls(
            location=str(entry.get("name") or entry.get("location_key") or ""),
            location_key=str(entry.get("location_key") or ""),
            observed_at_utc=parse_iso(entry.get("observed_at_utc")),
            temp_c=entry.get("temp_c"),
            feelslike_c=entry.get("feelslike_c"),
            uv=entry.get("uv"),
            wind_kph=entry.get("wind_kph"),
            precip_chance_next_hour=entry.get("precip_chance_next_hour"),
            precip_forecast_hour_utc=entry.get("precip_forecast_hour_utc"),
            condition_text=entry.get("condition_text"),
        )


@dataclass(frozen=True)
class Source:
    """A citation the user can follow back to the authority that issued it.

    Only ever populated from chunks that were actually retrieved for this
    request — never from the corpus at large, and never invented by a model.
    """

    chunk_id: str
    source_document_id: str
    authority: str
    title: str
    source_url: str


@dataclass(frozen=True)
class AdviceContent:
    """What a content provider returns: the words, and nothing else.

    Deliberately free of ids, timestamps and policy so that swapping the
    template provider for a retrieval-backed one cannot change the protocol.
    """

    title: str
    message: str
    evidence: tuple[Evidence, ...] = ()
    generation_method: str = "template-v1"
    # Added in phase two. Empty for the template provider, which is why the
    # card protocol stayed backward compatible: a consumer that ignores these
    # fields sees exactly the phase-one payload.
    sources: tuple[Source, ...] = ()
    advice_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class AdviceCard:
    recommendation_id: str
    location: str
    trigger_code: str
    severity: str
    title: str
    message: str
    evidence: tuple[Evidence, ...]
    weather_observed_at_utc: str | None
    generated_at_utc: str
    expires_at_utc: str
    generation_method: str
    weather_snapshot_id: str
    rule_version: str
    sources: tuple[Source, ...] = ()
    advice_codes: tuple[str, ...] = ()
    actions: tuple[Action, ...] = field(
        default_factory=lambda: (
            Action("dismiss", "知道了"),
            Action("mute", "今天不再提醒"),
        )
    )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["evidence"] = [asdict(item) for item in self.evidence]
        payload["actions"] = [asdict(item) for item in self.actions]
        payload["sources"] = [asdict(item) for item in self.sources]
        payload["advice_codes"] = list(self.advice_codes)
        return payload
