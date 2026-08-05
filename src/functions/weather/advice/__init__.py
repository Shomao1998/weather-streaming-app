"""Advice cards: deterministic, rule-driven suggestions built on the weather
snapshot the dashboard already reads.

Phase one is templates only — no model, no retrieval, no external service — so
that every sentence the system can emit is reviewable and every decision is
reproducible in a unit test. `providers.AdviceContentProvider` is the seam a
retrieval-backed provider slots into later without the rules, the frequency
policy or the API changing.
"""

from .models import (
    AdviceCard,
    AdviceContent,
    AdviceOutcome,
    AdviceTrigger,
    Evidence,
    FeedbackEvent,
    Severity,
    WeatherContext,
)
from .providers import AdviceContentProvider, TemplateAdviceProvider
from .service import AdviceResult, AdviceService, InvalidLocation, new_session_id

__all__ = [
    "AdviceCard",
    "AdviceContent",
    "AdviceContentProvider",
    "AdviceOutcome",
    "AdviceResult",
    "AdviceService",
    "AdviceTrigger",
    "Evidence",
    "FeedbackEvent",
    "InvalidLocation",
    "Severity",
    "TemplateAdviceProvider",
    "WeatherContext",
    "new_session_id",
]
