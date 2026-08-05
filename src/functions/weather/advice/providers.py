"""Content generation: a matched rule becomes words.

This is the seam phase two replaces. The service depends on the
`AdviceContentProvider` protocol and never on the template table, so a
retrieval-backed provider can be dropped in without the rule engine, the
frequency policy or the card API changing at all.
"""

from __future__ import annotations

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

    def generate(self, trigger: AdviceTrigger, weather: WeatherContext) -> AdviceContent: ...


# Deliberately fixed strings rather than anything generative. Phase one has to
# be reviewable and testable line by line: the same weather must always produce
# the same words, and a reviewer must be able to read every sentence the system
# can emit.
TEMPLATES: dict[AdviceTrigger, tuple[str, str]] = {
    AdviceTrigger.RAIN_EXPECTED: (
        "一小时内可能下雨",
        "未来一小时降水概率较高，出门记得带伞哦。",
    ),
    AdviceTrigger.HIGH_UV: (
        "紫外线较强",
        "紫外线指数偏高，注意防晒，避免长时间暴晒。",
    ),
    AdviceTrigger.EXTREME_HEAT: (
        "高温提醒",
        "气温较高，记得补水，尽量避开高温时段的户外活动。",
    ),
    AdviceTrigger.HIGH_WIND: (
        "风力较大",
        "室外风力较大，出行注意安全，留意高空坠物。",
    ),
}

FALLBACK = ("天气提醒", "当前天气需要留意，出门前请关注最新情况。")


class TemplateAdviceProvider:
    """Phase one: a lookup table, and nothing more."""

    name = "template-v1"

    def __init__(self, templates: dict[AdviceTrigger, tuple[str, str]] | None = None) -> None:
        self._templates = templates if templates is not None else TEMPLATES

    def generate(self, trigger: AdviceTrigger, weather: WeatherContext) -> AdviceContent:
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


def content_for(
    provider: AdviceContentProvider,
    match: RuleMatch,
    weather: WeatherContext,
) -> AdviceContent:
    """Generate copy for a match and attach the rule's evidence to it."""
    content = provider.generate(match.trigger, weather)
    return AdviceContent(
        title=content.title,
        message=content.message,
        evidence=content.evidence or match.evidence,
        generation_method=content.generation_method,
    )
