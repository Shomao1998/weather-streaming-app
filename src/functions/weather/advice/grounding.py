"""Validation of model output. Deterministic code, no model in the loop.

Everything that decides whether a generated card is allowed to reach a user
lives here, and none of it asks a model to grade itself. A judge model can be
wrong in exactly the same direction as the generator; a citation either
resolves to a chunk that was retrieved in this request or it does not.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from .models import WeatherContext
from .retrieval import RetrievedChunk

logger = logging.getLogger(__name__)

# The closed vocabulary of actions the system may recommend. A model cannot
# invent a new one: an unknown code fails validation and falls back. This is
# what keeps "advice" from drifting into medical or legal instruction.
ADVICE_CODES = frozenset(
    {
        # Heat
        "HYDRATE",
        "RESCHEDULE_STRENUOUS_ACTIVITY",
        "SEEK_COOLING",
        "WEAR_LIGHT_CLOTHING",
        "CHECK_ON_VULNERABLE_PEOPLE",
        # UV
        "REDUCE_SUN_EXPOSURE",
        "SEEK_SHADE",
        "USE_SUNSCREEN",
        "WEAR_PROTECTIVE_CLOTHING",
        # Wind
        "SHELTER_INDOORS",
        "AVOID_FALLING_HAZARDS",
        "DRIVE_WITH_CAUTION",
        "SECURE_LOOSE_OBJECTS",
        # Rain and flooding
        "CARRY_UMBRELLA",
        "ALLOW_EXTRA_TRAVEL_TIME",
        "AVOID_FLOODED_ROADS",
        "DO_NOT_WALK_THROUGH_FLOODWATER",
        # Air quality
        "LIMIT_OUTDOOR_EXERTION",
        "SENSITIVE_GROUPS_TAKE_CARE",
    }
)

MAX_TITLE_CHARS = 24
MAX_MESSAGE_CHARS = 90

# Numbers the model may not introduce. If a figure appears in the copy it must
# either match a weather fact we supplied or appear in a cited chunk; anything
# else is a fabricated measurement, which is the failure mode that would make
# this feature untrustworthy.
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")

BANNED_PATTERNS = (
    # Individual medical direction, dressed up as weather advice.
    re.compile(r"(诊断|处方|服药|就医建议|prescri|diagnos)", re.IGNORECASE),
    # Claiming an official warning level the rules did not assign.
    re.compile(r"(官方警报|气象台发布|red alert|official warning)", re.IGNORECASE),
    # Markdown, which the card renders as literal text.
    re.compile(r"(^#|\*\*|\[[^\]]*\]\()"),
)


@dataclass
class ValidationResult:
    ok: bool
    reason: str = ""
    failures: list[str] = field(default_factory=list)

    def fail(self, reason: str) -> ValidationResult:
        self.ok = False
        self.failures.append(reason)
        if not self.reason:
            self.reason = reason
        return self


@dataclass(frozen=True)
class GeneratedAdvice:
    """The model's structured output, after parsing but before validation."""

    title: str
    message: str
    advice_codes: tuple[str, ...]
    supporting_chunk_ids: tuple[str, ...]
    abstained: bool = False


def parse_model_output(raw: str) -> GeneratedAdvice | None:
    """Parse strict JSON. Anything else is a failure, not something to repair.

    Coaxing malformed output into shape is how a validator starts accepting
    things it should reject; a fallback to the template is cheaper and safer.
    """
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    if payload.get("abstain") is True or payload.get("cannot_generate") is True:
        return GeneratedAdvice("", "", (), (), abstained=True)

    title = payload.get("title")
    message = payload.get("message")
    if not isinstance(title, str) or not isinstance(message, str):
        return None

    codes = payload.get("advice_codes")
    chunks = payload.get("supporting_chunk_ids")
    if not isinstance(codes, list) or not isinstance(chunks, list):
        return None

    return GeneratedAdvice(
        title=title.strip(),
        message=message.strip(),
        advice_codes=tuple(str(c).strip() for c in codes),
        supporting_chunk_ids=tuple(str(c).strip() for c in chunks),
    )


def _weather_numbers(weather: WeatherContext) -> set[str]:
    """Every figure the model was given, in the forms it might restate them."""
    values: set[str] = set()
    for value in (
        weather.temp_c,
        weather.feelslike_c,
        weather.uv,
        weather.wind_kph,
        weather.precip_chance_next_hour,
    ):
        if value is None:
            continue
        number = float(value)
        values.add(f"{number:g}")
        values.add(f"{round(number):g}")
        values.add(f"{number:.1f}")
    return values


def validate(
    advice: GeneratedAdvice,
    *,
    retrieved: list[RetrievedChunk],
    weather: WeatherContext,
) -> ValidationResult:
    """Every check that must pass before a generated card can be shown."""
    result = ValidationResult(ok=True)
    retrieved_ids = {c.chunk_id for c in retrieved}
    # A chunk retrieved a moment ago can still have been disabled since the
    # index was loaded; re-assert rather than trusting the retrieval.
    citable_ids = {c.chunk_id for c in retrieved if c.chunk.is_effective()}

    if advice.abstained:
        return result.fail("model abstained")

    if not advice.title or not advice.message:
        result.fail("empty title or message")
    if len(advice.title) > MAX_TITLE_CHARS:
        result.fail(f"title is {len(advice.title)} chars, limit {MAX_TITLE_CHARS}")
    if len(advice.message) > MAX_MESSAGE_CHARS:
        result.fail(f"message is {len(advice.message)} chars, limit {MAX_MESSAGE_CHARS}")

    for pattern in BANNED_PATTERNS:
        if pattern.search(advice.title) or pattern.search(advice.message):
            result.fail("output matched a banned pattern")
            break

    if not advice.advice_codes:
        result.fail("no advice codes")
    unknown = [c for c in advice.advice_codes if c not in ADVICE_CODES]
    if unknown:
        result.fail(f"unknown advice codes: {unknown}")

    if not advice.supporting_chunk_ids:
        result.fail("no supporting citations")
    unsupported = [c for c in advice.supporting_chunk_ids if c not in retrieved_ids]
    if unsupported:
        result.fail(f"citations not in this retrieval: {unsupported}")
    disabled = [
        c for c in advice.supporting_chunk_ids if c in retrieved_ids and c not in citable_ids
    ]
    if disabled:
        result.fail(f"citations resolve to disabled sources: {disabled}")

    allowed_numbers = _weather_numbers(weather)
    corpus_numbers = {n for c in retrieved for n in NUMBER_RE.findall(c.chunk.content)}
    for number in NUMBER_RE.findall(f"{advice.title} {advice.message}"):
        if number not in allowed_numbers and number not in corpus_numbers:
            result.fail(f"figure '{number}' appears in neither the weather facts nor the sources")
            break

    return result
