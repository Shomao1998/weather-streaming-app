"""Orchestration: snapshot → freshness → rules → policy → content → card.

The only place that knows the whole sequence. Everything it calls is either a
pure function or a protocol, so the ordering can be tested end to end without
Azure, and any single step can be replaced without touching the others.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..config import Settings, get_settings
from . import frequency, providers, rules
from . import repository as advice_repository
from .models import (
    AdviceCard,
    AdviceOutcome,
    AdviceTrigger,
    FeedbackEvent,
    Severity,
    WeatherContext,
)
from .repository import AdviceStateRepository

logger = logging.getLogger(__name__)


class InvalidLocation(ValueError):
    """The caller asked about a location this deployment does not collect."""


@dataclass(frozen=True)
class AdviceResult:
    """A card, or a documented reason there isn't one.

    Never an exception for the ordinary "nothing to say" paths: no card is the
    most common correct answer, and callers should not need a try/except to
    handle the normal case.
    """

    outcome: AdviceOutcome
    card: AdviceCard | None = None
    detail: str = ""

    @property
    def has_card(self) -> bool:
        return self.card is not None


def _log(
    outcome: AdviceOutcome,
    ctx: WeatherContext | None,
    settings: Settings,
    **extra: Any,
) -> None:
    dimensions = {
        "outcome": str(outcome),
        "location": ctx.location if ctx else extra.get("location", ""),
        "rule_version": settings.advice.rule_version,
        **extra,
    }
    logger.info("ADVICE_%s %s", str(outcome).upper(), dimensions,
                extra={"custom_dimensions": dimensions})


class AdviceService:
    """Builds at most one card per request."""

    def __init__(
        self,
        settings: Settings | None = None,
        provider: providers.AdviceContentProvider | None = None,
        repository: AdviceStateRepository | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._provider = provider or providers.TemplateAdviceProvider()
        self._repository = repository or advice_repository.get_repository(self._settings)

    def _find(self, snapshot: dict[str, Any], location: str) -> dict[str, Any] | None:
        wanted = location.strip().casefold()
        for entry in snapshot.get("locations", []):
            name = str(entry.get("name") or "").casefold()
            key = str(entry.get("location_key") or "").casefold()
            if wanted in (name, key):
                return entry
        return None

    def build(
        self,
        snapshot: dict[str, Any],
        location: str,
        session_id: str,
        now: datetime | None = None,
        question: str | None = None,
    ) -> AdviceResult:
        settings = self._settings
        now = now or datetime.now(UTC)

        if not settings.advice.enabled:
            _log(AdviceOutcome.DISABLED, None, settings, location=location)
            return AdviceResult(AdviceOutcome.DISABLED)

        entry = self._find(snapshot, location)
        if entry is None:
            # A typo must be a 400, not an empty card that looks like calm
            # weather — otherwise a broken client fails silently forever.
            raise InvalidLocation(location)

        ctx = WeatherContext.from_serving_location(entry)

        age = ctx.age(now)
        if age is None or age > settings.advice.max_weather_age_minutes:
            _log(AdviceOutcome.STALE_WEATHER, ctx, settings, age_minutes=round(age or -1, 1))
            return AdviceResult(AdviceOutcome.STALE_WEATHER, detail="weather snapshot is stale")

        match = rules.top_match(ctx, settings.advice)
        if match is None:
            _log(AdviceOutcome.NO_RULE_MATCHED, ctx, settings)
            return AdviceResult(AdviceOutcome.NO_RULE_MATCHED)

        key = frequency.dedup_key(
            ctx.location_key, match.trigger, ctx.snapshot_id, settings.advice.rule_version
        )
        state = self._repository.load(session_id)
        decision = frequency.decide(
            key=key,
            trigger=match.trigger,
            severity=match.severity,
            state=state,
            now=now,
            settings=settings.advice,
        )
        if not decision.allowed:
            _log(decision.outcome, ctx, settings, trigger=str(match.trigger),
                 reason=decision.reason)
            return AdviceResult(decision.outcome, detail=decision.reason)

        try:
            content = providers.content_for(self._provider, match, ctx, question)
        except Exception as exc:
            # A content provider is the part most likely to change and the part
            # most likely to fail once it stops being a lookup table. Its
            # failure must degrade to "no advice", never to a broken page.
            logger.exception("Advice content provider failed.")
            _log(AdviceOutcome.PROVIDER_FAILURE, ctx, settings, trigger=str(match.trigger),
                 provider=getattr(self._provider, "name", "unknown"))
            return AdviceResult(AdviceOutcome.PROVIDER_FAILURE, detail=str(exc))

        card = AdviceCard(
            recommendation_id=key,
            location=ctx.location,
            trigger_code=str(match.trigger),
            severity=str(match.severity),
            title=content.title,
            message=content.message,
            evidence=content.evidence,
            weather_observed_at_utc=(
                ctx.observed_at_utc.isoformat().replace("+00:00", "Z")
                if ctx.observed_at_utc
                else None
            ),
            generated_at_utc=now.isoformat().replace("+00:00", "Z"),
            expires_at_utc=frequency.expires_at(now, settings.advice)
            .isoformat()
            .replace("+00:00", "Z"),
            generation_method=content.generation_method,
            weather_snapshot_id=ctx.snapshot_id,
            rule_version=settings.advice.rule_version,
            sources=content.sources,
            advice_codes=content.advice_codes,
        )

        self._repository.record_shown(
            session_id,
            frequency.ShownRecord(
                dedup_key=key,
                trigger=str(match.trigger),
                severity=str(match.severity),
                shown_at=now,
            ),
        )
        _log(AdviceOutcome.GENERATED, ctx, settings, trigger=str(match.trigger),
             recommendation_id=key, generation_method=content.generation_method,
             severity=str(match.severity))
        return AdviceResult(AdviceOutcome.GENERATED, card=card)

    def record_feedback(
        self, payload: dict[str, Any], now: datetime | None = None
    ) -> dict[str, Any]:
        """Validate and log one feedback event; apply it when it changes policy."""
        now = now or datetime.now(UTC)

        event = str(payload.get("event") or "").strip().lower()
        if event not in {e.value for e in FeedbackEvent}:
            raise ValueError(f"unknown event '{event}'")

        session_id = str(payload.get("session_id") or "").strip()
        if not session_id:
            raise ValueError("session_id is required")

        trigger = str(payload.get("trigger_code") or "").strip()
        record = {
            "recommendation_id": str(payload.get("recommendation_id") or ""),
            "trigger_code": trigger,
            "location": str(payload.get("location") or ""),
            "weather_snapshot_id": str(payload.get("weather_snapshot_id") or ""),
            "generation_method": str(payload.get("generation_method") or ""),
            "event": event,
            "event_at_utc": now.isoformat().replace("+00:00", "Z"),
            "session_id": session_id,
            "rule_version": self._settings.advice.rule_version,
        }

        if event == FeedbackEvent.MUTED and trigger:
            self._repository.record_mute(
                session_id,
                frequency.MuteRecord(
                    trigger=trigger,
                    muted_until=frequency.mute_until(now, self._settings.advice),
                ),
            )

        logger.info("ADVICE_FEEDBACK %s", record, extra={"custom_dimensions": record})
        return record


def new_session_id() -> str:
    """An opaque, anonymous id the client keeps for the browser session."""
    return uuid.uuid4().hex


__all__ = [
    "AdviceCard",
    "AdviceOutcome",
    "AdviceResult",
    "AdviceService",
    "AdviceTrigger",
    "InvalidLocation",
    "Severity",
    "new_session_id",
]
