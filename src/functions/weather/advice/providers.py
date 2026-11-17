"""Content generation: a matched rule becomes words.

This is the seam phase two replaces. The service depends on the
`AdviceContentProvider` protocol and never on the template table, so a
retrieval-backed provider can be dropped in without the rule engine, the
frequency policy or the card API changing at all.
"""

from __future__ import annotations

import inspect
import logging
from typing import Protocol

from .models import AdviceContent, AdviceTrigger, WeatherContext
from .rules import RuleMatch

logger = logging.getLogger(__name__)


class AdviceContentProvider(Protocol):
    """Turns a matched rule into a title and a message.

    Implementations must be side-effect free from the caller's point of view
    and must not raise for ordinary input — the service treats an exception as
    a provider failure and degrades to showing no card.
    """

    name: str

    def generate(
        self,
        trigger: AdviceTrigger,
        weather: WeatherContext,
        question: str | None = None,
    ) -> AdviceContent: ...


# Deliberately fixed strings rather than anything generative. Phase one has to
# be reviewable and testable line by line: the same weather must always produce
# the same words, and a reviewer must be able to read every sentence the system
# can emit.
TEMPLATES: dict[AdviceTrigger, tuple[str, str]] = {
    AdviceTrigger.RAIN_EXPECTED: (
        "Rain likely soon",
        "There's a good chance of rain within the hour — take an umbrella.",
    ),
    AdviceTrigger.HIGH_UV: (
        "High UV",
        "The UV index is high — use sun protection and avoid long exposure.",
    ),
    AdviceTrigger.EXTREME_HEAT: (
        "High heat",
        "It's hot out — stay hydrated and avoid outdoor activity at peak heat.",
    ),
    AdviceTrigger.HIGH_WIND: (
        "Strong wind",
        "Winds are strong outside — take care and watch for falling debris.",
    ),
}

FALLBACK = ("Weather notice", "Conditions warrant attention — check the latest before heading out.")


class TemplateAdviceProvider:
    """Phase one: a lookup table, and nothing more."""

    name = "template-v1"

    def __init__(self, templates: dict[AdviceTrigger, tuple[str, str]] | None = None) -> None:
        self._templates = templates if templates is not None else TEMPLATES

    def generate(
        self,
        trigger: AdviceTrigger,
        weather: WeatherContext,
        question: str | None = None,
    ) -> AdviceContent:
        # A template cannot answer a free-text question, so it ignores it and
        # answers the hazard. That is the honest degradation.
        title, message = self._templates.get(trigger, FALLBACK)
        if trigger not in self._templates:
            # Reaching the fallback means a rule was added without copy. Worth
            # a log line: the card is still valid, but it is generic.
            logger.warning("No template for trigger %s; using the fallback copy.", trigger)
        return AdviceContent(
            title=title,
            message=message,
            # Evidence comes from the rule that matched, not from the provider:
            # the numbers are facts, not phrasing, and must stay identical
            # whichever provider is in use.
            evidence=(),
            generation_method=self.name,
        )


def _accepts_question(func: object) -> bool:
    try:
        signature = inspect.signature(func)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in signature.parameters.values()):
        return True
    return len(signature.parameters) >= 3


def content_for(
    provider: AdviceContentProvider,
    match: RuleMatch,
    weather: WeatherContext,
    question: str | None = None,
) -> AdviceContent:
    """Generate copy for a match and attach the rule's evidence to it.

    Phase two widened the provider protocol with an optional `question`. A
    provider written against the phase-one two-argument signature must keep
    working untouched, so the extra argument is passed only when the callee
    actually accepts it. The phase-one test that supplies a two-argument
    provider is left unchanged on purpose: it is the compatibility check.
    """
    if question is not None and _accepts_question(provider.generate):
        content = provider.generate(match.trigger, weather, question)
    else:
        content = provider.generate(match.trigger, weather)
    return AdviceContent(
        title=content.title,
        message=content.message,
        # The rule's evidence always wins. A generated sentence may not
        # introduce or alter the figure the card displays.
        evidence=content.evidence or match.evidence,
        generation_method=content.generation_method,
        sources=content.sources,
        advice_codes=content.advice_codes,
    )
