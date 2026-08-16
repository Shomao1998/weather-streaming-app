"""Advice cards: deterministic, rule-driven suggestions built on the weather
snapshot the dashboard already reads.

Phase one is templates only — no model, no retrieval, no external service — so
that every sentence the system can emit is reviewable and every decision is
reproducible in a unit test. `providers.AdviceContentProvider` is the seam a
retrieval-backed provider slots into later without the rules, the frequency
policy or the API changing.

`factory.get_provider` is deliberately *not* re-exported here. Re-exporting
binds a second name to the same function, and a caller that imports the alias
cannot be redirected by patching the module that defines it — the mistake that
made the phase-one advice API tests reach real storage. Callers use
`advice.factory.get_provider()`.

Phase two adds `rag.RagAdviceProvider` through exactly that seam. What changed
is the wording and the citations; what did not change is which card appears,
how severe it is, when it is suppressed and when it expires. Retrieval and
generation are strictly downstream of every decision that matters.
"""

from .models import (
    AdviceCard,
    AdviceContent,
    AdviceOutcome,
    AdviceTrigger,
    Evidence,
    FeedbackEvent,
    Severity,
    Source,
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
    "Source",
    "TemplateAdviceProvider",
    "WeatherContext",
    "new_session_id",
]
