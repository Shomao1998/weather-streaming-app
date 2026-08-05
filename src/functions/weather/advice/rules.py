"""The rule engine: weather facts in, matched triggers out.

Pure functions over `WeatherContext` and `AdviceSettings`. No storage, no
clock, no wording — a rule decides *whether* something is worth saying and
supplies the evidence; a content provider decides *how* to say it.

Every threshold comes from settings. A rule that hard-codes a number is a rule
nobody can tune without a deployment.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ..config import AdviceSettings
from .models import AdviceTrigger, Evidence, Severity, WeatherContext


@dataclass(frozen=True)
class RuleMatch:
    trigger: AdviceTrigger
    severity: Severity
    priority: int
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True)
class Rule:
    """One trigger.

    `priority` orders rules against each other when several fire at once.
    Severity is *not* used for ordering: a rule can be important to show first
    while still only warranting an `info` presentation. Keeping the two
    separate is what lets an official severe-weather warning slot in later at
    the top without every existing rule having to be re-graded.
    """

    trigger: AdviceTrigger
    priority: int
    evaluate: Callable[[WeatherContext, AdviceSettings], RuleMatch | None]


def _pct(value: float | int) -> str:
    return f"{round(float(value))}%"


def _rain(ctx: WeatherContext, settings: AdviceSettings) -> RuleMatch | None:
    chance = ctx.precip_chance_next_hour
    # None means the hourly forecast did not cover the look-ahead window.
    # "Unknown" is not "no rain", so the rule declines rather than guessing.
    if chance is None or chance < settings.rain_chance_percent:
        return None
    return RuleMatch(
        trigger=AdviceTrigger.RAIN_EXPECTED,
        severity=Severity.INFO,
        priority=30,
        evidence=(Evidence("降水概率", _pct(chance)),),
    )


def _uv(ctx: WeatherContext, settings: AdviceSettings) -> RuleMatch | None:
    if ctx.uv is None or ctx.uv < settings.uv_index:
        return None
    return RuleMatch(
        trigger=AdviceTrigger.HIGH_UV,
        severity=Severity.WARNING,
        priority=20,
        evidence=(Evidence("紫外线指数", f"{ctx.uv:g}"),),
    )


def _heat(ctx: WeatherContext, settings: AdviceSettings) -> RuleMatch | None:
    if ctx.temp_c is None or ctx.temp_c < settings.heat_c:
        return None
    evidence = [Evidence("气温", f"{ctx.temp_c:g}°C")]
    if ctx.feelslike_c is not None:
        evidence.append(Evidence("体感温度", f"{ctx.feelslike_c:g}°C"))
    return RuleMatch(
        trigger=AdviceTrigger.EXTREME_HEAT,
        severity=Severity.WARNING,
        priority=10,
        evidence=tuple(evidence),
    )


def _wind(ctx: WeatherContext, settings: AdviceSettings) -> RuleMatch | None:
    if ctx.wind_kph is None or ctx.wind_kph < settings.wind_kph:
        return None
    return RuleMatch(
        trigger=AdviceTrigger.HIGH_WIND,
        severity=Severity.WARNING,
        priority=15,
        evidence=(Evidence("风速", f"{ctx.wind_kph:g} km/h"),),
    )


# Lower priority number wins. Heat outranks wind, wind outranks UV, and rain is
# last: an umbrella is useful advice, but not ahead of a heat warning.
RULES: tuple[Rule, ...] = (
    Rule(AdviceTrigger.EXTREME_HEAT, 10, _heat),
    Rule(AdviceTrigger.HIGH_WIND, 15, _wind),
    Rule(AdviceTrigger.HIGH_UV, 20, _uv),
    Rule(AdviceTrigger.RAIN_EXPECTED, 30, _rain),
)


def evaluate(ctx: WeatherContext, settings: AdviceSettings) -> list[RuleMatch]:
    """Every rule that fires, most important first."""
    matches = [m for rule in RULES if (m := rule.evaluate(ctx, settings)) is not None]
    return sorted(matches, key=lambda m: m.priority)


def top_match(ctx: WeatherContext, settings: AdviceSettings) -> RuleMatch | None:
    """The single rule a card should be built from, or None for quiet weather."""
    matches = evaluate(ctx, settings)
    return matches[0] if matches else None


def all_triggers() -> Sequence[AdviceTrigger]:
    return tuple(rule.trigger for rule in RULES)
