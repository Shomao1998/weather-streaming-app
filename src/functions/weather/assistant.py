"""The dashboard assistant: answers a user's free-text weather question.

Two kinds of question, routed by keyword — deterministically, with no model:

* **forecast** ("会不会下雨", "明天几度", "放晴了吗") is answered from the daily
  forecast the serving layer now carries. A data lookup, not a guess.
* **guidance** ("下雨天注意什么", "该带什么防护") is answered by retrieving the
  official safety corpus. Implemented in a later step; until then a forecast-
  only reply is returned.

Everything here is a lookup or a template over data the pipeline already
produced. There is no generation, no external call, and no cost. A question the
router cannot place gets an honest statement of what the assistant *can* answer,
never a fabricated one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

# Words that mark a question as asking for forecast *data* — a condition or a
# measurement, usually paired with a day. Kept separate from the guidance words
# below so "下雨天要注意什么" (guidance) does not read as "会下雨吗" (data).
FORECAST_WORDS = (
    "下雨", "会不会", "下雪", "几度", "多少度", "多热", "多冷", "温度", "气温",
    "放晴", "晴天", "晴不晴", "多云", "阴天", "天气", "预报", "湿度", "刮风",
    "风大", "紫外线", "热吗", "冷吗", "下雨吗", "会下", "rain", "temperature",
    "forecast", "weather", "hot", "cold",
)

GUIDANCE_WORDS = (
    "注意", "该带", "带什么", "怎么办", "需要准备", "准备什么", "防护", "防晒",
    "安全", "建议", "穿什么", "要不要", "如何", "怎么防", "注意事项",
    "protect", "should i", "how to", "what to",
)

# Day references, resolved relative to "today" in the location's data. Kept
# small and explicit; "周末/下周" and the like are deliberately out of scope
# rather than guessed at.
_TODAY_WORDS = ("今天", "今日", "现在", "today", "now")
_TOMORROW_WORDS = ("明天", "明日", "tomorrow")
_DAY_AFTER_WORDS = ("后天", "day after")


@dataclass
class Answer:
    kind: str  # "forecast" | "guidance" | "unknown"
    location: str = ""
    title: str = ""
    message: str = ""
    detail: list[dict[str, str]] = field(default_factory=list)
    sources: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "location": self.location,
            "title": self.title,
            "message": self.message,
        }
        if self.detail:
            payload["detail"] = self.detail
        if self.sources:
            payload["sources"] = self.sources
        return payload


def classify(question: str) -> str:
    """Route a question to a handler. Guidance wins ties, because a guidance
    phrase ("下雨天注意什么") almost always contains a forecast word too."""
    q = (question or "").lower()
    if any(word in q for word in GUIDANCE_WORDS):
        return "guidance"
    if any(word in q for word in FORECAST_WORDS):
        return "forecast"
    return "unknown"


def _target_date(question: str, now: datetime) -> tuple[date | None, str]:
    """The day the question is about, and a Chinese label for it.

    None means "no specific day named" — the caller then shows a short
    multi-day outlook rather than picking one.
    """
    q = question or ""
    today = now.date()
    if any(w in q for w in _DAY_AFTER_WORDS):
        return today + timedelta(days=2), "后天"
    if any(w in q for w in _TOMORROW_WORDS):
        return today + timedelta(days=1), "明天"
    if any(w in q for w in _TODAY_WORDS):
        return today, "今天"
    return None, ""


def _rain_phrase(chance: Any) -> str:
    if chance is None:
        return ""
    try:
        c = int(chance)
    except (TypeError, ValueError):
        return ""
    if c >= 80:
        return "很可能下雨，出门带把伞。"
    if c >= 50:
        return "有较大机会下雨，建议带伞。"
    if c >= 20:
        return "可能有零星降雨，带把伞更稳妥。"
    return "基本不会下雨。"


def _day_label(entry_date: str, now: datetime) -> str:
    try:
        d = date.fromisoformat(entry_date)
    except (TypeError, ValueError):
        return entry_date or ""
    delta = (d - now.date()).days
    return {0: "今天", 1: "明天", 2: "后天"}.get(delta, entry_date)


def _format_day(location: str, day: dict[str, Any], label: str, now: datetime) -> Answer:
    label = label or _day_label(day.get("date", ""), now)
    chance = day.get("chance_of_rain")
    condition = day.get("condition_text") or ""
    hi, lo = day.get("maxtemp_c"), day.get("mintemp_c")

    bits = []
    if condition:
        bits.append(condition)
    if hi is not None and lo is not None:
        bits.append(f"最高 {hi:g}°C / 最低 {lo:g}°C")
    if chance is not None:
        bits.append(f"降水概率 {chance}%")
    headline = "，".join(bits) if bits else "暂无预报数据"

    rain = _rain_phrase(chance)
    message = f"{headline}。{rain}".strip("。 ") + ("。" if headline != "暂无预报数据" else "")

    detail = []
    if hi is not None:
        detail.append({"label": "最高", "value": f"{hi:g}°C"})
    if lo is not None:
        detail.append({"label": "最低", "value": f"{lo:g}°C"})
    if chance is not None:
        detail.append({"label": "降水概率", "value": f"{chance}%"})
    if day.get("uv") is not None:
        detail.append({"label": "紫外线", "value": f"{day['uv']:g}"})

    return Answer(
        kind="forecast",
        location=location,
        title=f"{location} · {label}",
        message=message,
        detail=detail,
    )


def answer_forecast(location: str, forecast: list[dict[str, Any]], question: str,
                    now: datetime | None = None) -> Answer:
    now = now or datetime.now(UTC)
    if not forecast:
        return Answer(
            kind="forecast",
            location=location,
            title=location,
            message="暂时没有拿到未来的预报数据，稍后再问一次试试。",
        )

    target, label = _target_date(question, now)
    if target is not None:
        wanted = target.isoformat()
        match = next((d for d in forecast if d.get("date") == wanted), None)
        if match is None:
            horizon = forecast[-1].get("date")
            return Answer(
                kind="forecast",
                location=location,
                title=f"{location} · {label}",
                message=f"预报只到 {horizon}，还看不到{label}。",
            )
        return _format_day(location, match, label, now)

    # No day named — a short outlook over the days we have.
    lines = []
    for day in forecast[:3]:
        lbl = _day_label(day.get("date", ""), now)
        chance = day.get("chance_of_rain")
        cond = day.get("condition_text") or ""
        piece = f"{lbl}：{cond}"
        if chance is not None:
            piece += f"，降水 {chance}%"
        lines.append(piece)
    return Answer(
        kind="forecast",
        location=location,
        title=f"{location} · 未来几天",
        message="；".join(lines) + "。" if lines else "暂无预报数据。",
    )


# Placeholder until the guidance retrieval path lands. Kept explicit so the
# endpoint has one place to swap in the real handler.
def answer_guidance(location: str, question: str, weather: dict[str, Any]) -> Answer | None:
    return None


def answer(snapshot_location: dict[str, Any], location_name: str, question: str,
           now: datetime | None = None) -> Answer:
    """Route and answer. Never raises for a well-formed call; an unanswerable
    question returns an honest ``unknown`` answer."""
    kind = classify(question)

    if kind == "forecast":
        return answer_forecast(
            location_name, snapshot_location.get("forecast") or [], question, now
        )

    if kind == "guidance":
        guided = answer_guidance(location_name, question, snapshot_location)
        if guided is not None:
            return guided
        # Until guidance retrieval lands, be honest about the current scope.
        return Answer(
            kind="unknown",
            location=location_name,
            title=location_name,
            message="这类防护建议我正在接入。现在我可以答天气预报，"
            "比如「明天会下雨吗」「后天几度」。",
        )

    return Answer(
        kind="unknown",
        location=location_name,
        title=location_name,
        message="我可以答天气预报，比如「明天会下雨吗」「今天几度」。",
    )
