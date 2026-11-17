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

import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# Words that mark a question as asking for forecast *data* — a condition or a
# measurement, usually paired with a day. Kept separate from the guidance words
# below so "下雨天要注意什么" (guidance) does not read as "会下雨吗" (data).
FORECAST_WORDS = (
    "下雨", "会不会", "下雪", "几度", "多少度", "多热", "多冷", "温度", "气温",
    "放晴", "晴天", "晴不晴", "多云", "阴天", "天气", "预报", "湿度", "刮风",
    "风大", "紫外线", "热吗", "冷吗", "下雨吗", "会下", "rain", "temperature",
    "forecast", "weather", "hot", "cold",
    # Japanese forecast words (kanji differ from the simplified-Chinese forms).
    "天気", "気温", "雪", "晴れ", "降り", "雨", "予報",
)

GUIDANCE_WORDS = (
    "注意", "小心", "该带", "带什么", "怎么办", "需要准备", "准备什么", "防护",
    "防晒", "安全", "建议", "穿什么", "要不要", "能不能", "可不可以", "如何",
    "怎么防", "注意事项", "protect", "should i", "how to", "what to",
    # Japanese guidance markers.
    "気をつけ", "対策", "気を付け",
)

# Day references, resolved relative to "today" in the location's data. Kept
# small and explicit; "周末/下周" and the like are deliberately out of scope
# rather than guessed at. The Chinese 今日/明日 forms double as Japanese.
_TODAY_WORDS = ("今天", "今日", "现在", "本日", "today", "now")
_TOMORROW_WORDS = ("明天", "明日", "tomorrow")
_DAY_AFTER_WORDS = ("后天", "明後日", "あさって", "day after")


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


def _lang(question: str) -> str:
    """Which language to answer in — English by default, Chinese or Japanese only
    to match the person asking. Japanese is checked first: kana (hiragana /
    katakana) is a definitive Japanese signal, and Japanese writes its kanji in
    the same Unicode block as Chinese, so a kana hit must win before the Han
    check. Then Chinese for Han characters, else English."""
    has_han = False
    for ch in question or "":
        if "぀" <= ch <= "ヿ" or "ｦ" <= ch <= "ﾟ":  # hiragana, katakana, half-width kana
            return "ja"
        if "一" <= ch <= "鿿":  # CJK unified ideographs (Han)
            has_han = True
    return "zh" if has_han else "en"


# Day labels, by language, for the answer title.
_DAY_LABELS = {
    "today": {"en": "today", "zh": "今天", "ja": "今日"},
    "tomorrow": {"en": "tomorrow", "zh": "明天", "ja": "明日"},
    "day_after": {"en": "the day after tomorrow", "zh": "后天", "ja": "明後日"},
}

# Punctuation that differs by language: the separator inside a headline list, the
# join between the headline and the rain phrase, the sentence terminator, and the
# separator between day lines in the multi-day outlook.
_SEP = {"en": ", ", "zh": "，", "ja": "、"}
_MID = {"en": ". ", "zh": "。", "ja": "。"}
_END = {"en": ".", "zh": "。", "ja": "。"}
_OUTLOOK_SEP = {"en": "; ", "zh": "；", "ja": "、"}

# Every templated phrase the forecast answer can emit, per language. The
# retrieved guidance passages stay in their original official English; only the
# surrounding copy is translated here.
_T = {
    "en": {
        "hi_lo": "High {hi:g}°C / Low {lo:g}°C",
        "chance": "Chance of rain {chance}%",
        "no_data": "No forecast data",
        "lbl_hi": "High", "lbl_lo": "Low", "lbl_rain": "Chance of rain", "lbl_uv": "UV",
        "no_forecast_now": "No forecast data available right now — please try again shortly.",
        "horizon": "The forecast only reaches {horizon}, so {label} isn't available yet.",
        "outlook_piece": "{lbl}: {cond}",
        "outlook_rain": ", rain {chance}%",
        "outlook_title": "Next few days",
        "rain": ("Rain is very likely — take an umbrella.",
                 "There's a good chance of rain — bring an umbrella.",
                 "There may be a little rain — an umbrella is a safe bet.",
                 "Rain is unlikely."),
        "guidance_na": "I can't answer that kind of guidance question yet. I can answer "
                       "the forecast — for example, \"Will it rain tomorrow?\"",
        "unknown": "I can answer the forecast — for example, \"Will it rain tomorrow?\" "
                   "or \"How hot is it today?\"",
    },
    "zh": {
        "hi_lo": "最高 {hi:g}°C / 最低 {lo:g}°C",
        "chance": "降水概率 {chance}%",
        "no_data": "暂无预报数据",
        "lbl_hi": "最高", "lbl_lo": "最低", "lbl_rain": "降水概率", "lbl_uv": "紫外线",
        "no_forecast_now": "暂时没有拿到未来的预报数据，稍后再问一次试试。",
        "horizon": "预报只到 {horizon}，还看不到{label}。",
        "outlook_piece": "{lbl}：{cond}",
        "outlook_rain": "，降水 {chance}%",
        "outlook_title": "未来几天",
        "rain": ("很可能下雨，出门带把伞。", "有较大机会下雨，建议带伞。",
                 "可能有零星降雨，带把伞更稳妥。", "基本不会下雨。"),
        "guidance_na": "这类防护建议我暂时答不了。现在我可以答天气预报，"
                       "比如「明天会下雨吗」「后天几度」。",
        "unknown": "我可以答天气预报，比如「明天会下雨吗」「今天几度」。",
    },
    "ja": {
        "hi_lo": "最高 {hi:g}°C / 最低 {lo:g}°C",
        "chance": "降水確率 {chance}%",
        "no_data": "予報データがありません",
        "lbl_hi": "最高", "lbl_lo": "最低", "lbl_rain": "降水確率", "lbl_uv": "UV",
        "no_forecast_now": "現在、予報データを取得できません。後ほどもう一度お試しください。",
        "horizon": "予報は {horizon} までです。{label}はまだ表示できません。",
        "outlook_piece": "{lbl}：{cond}",
        "outlook_rain": "、降水 {chance}%",
        "outlook_title": "今後数日",
        "rain": ("雨の可能性がとても高いです。傘をお持ちください。",
                 "雨が降る可能性が高いです。傘をおすすめします。",
                 "小雨が降るかもしれません。傘があると安心です。",
                 "雨の可能性は低いです。"),
        "guidance_na": "そのような対策に関する質問にはまだお答えできません。"
                       "天気予報についてはお答えできます（例：「明日は雨が降りますか」）。",
        "unknown": "天気予報についてお答えできます"
                   "（例：「明日は雨が降りますか」「今日の気温は？」）。",
    },
}


def classify(question: str) -> str:
    """Route a question to a handler.

    Order matters. An explicit help-seeking phrase ("下雨天注意什么") is
    guidance even though it contains a forecast word. A bare data question
    ("会不会下雨") is forecast. A question that only names a hazard situation
    ("能不能蹚水过去") has neither marker but is still asking for guidance, so a
    hazard keyword with no data question falls to guidance last.
    """
    q = (question or "").lower()
    if any(word in q for word in GUIDANCE_WORDS):
        return "guidance"
    if any(word in q for word in FORECAST_WORDS):
        return "forecast"
    if any(word in q for words in _HAZARD_KEYWORDS.values() for word in words):
        return "guidance"
    return "unknown"


def _target_date(question: str, now: datetime) -> tuple[date | None, str]:
    """The day the question is about, and a label *key* for it ("today" /
    "tomorrow" / "day_after"). None means no specific day was named — the caller
    then shows a short multi-day outlook rather than picking one.
    """
    q = question or ""
    today = now.date()
    if any(w in q for w in _DAY_AFTER_WORDS):
        return today + timedelta(days=2), "day_after"
    if any(w in q for w in _TOMORROW_WORDS):
        return today + timedelta(days=1), "tomorrow"
    if any(w in q for w in _TODAY_WORDS):
        return today, "today"
    return None, ""


def _rain_phrase(chance: Any, lang: str) -> str:
    if chance is None:
        return ""
    try:
        c = int(chance)
    except (TypeError, ValueError):
        return ""
    phrases = _T[lang]["rain"]
    if c >= 80:
        return phrases[0]
    if c >= 50:
        return phrases[1]
    if c >= 20:
        return phrases[2]
    return phrases[3]


def _day_label_key(entry_date: str, now: datetime) -> tuple[str, str]:
    """(label_key, fallback_text). The key resolves to language text; the
    fallback is the raw date for days beyond the day after tomorrow."""
    try:
        d = date.fromisoformat(entry_date)
    except (TypeError, ValueError):
        return "", entry_date or ""
    delta = (d - now.date()).days
    key = {0: "today", 1: "tomorrow", 2: "day_after"}.get(delta, "")
    return key, entry_date


def _label_text(key: str, fallback: str, lang: str) -> str:
    if key:
        return _DAY_LABELS[key][lang]
    return fallback


def _format_day(location: str, day: dict[str, Any], label_key: str,
                now: datetime, lang: str) -> Answer:
    if not label_key:
        label_key, fallback = _day_label_key(day.get("date", ""), now)
        label = _label_text(label_key, fallback, lang)
    else:
        label = _DAY_LABELS[label_key][lang]
    chance = day.get("chance_of_rain")
    condition = day.get("condition_text") or ""
    hi, lo = day.get("maxtemp_c"), day.get("mintemp_c")

    t = _T[lang]
    bits = []
    if condition:
        bits.append(condition)
    if hi is not None and lo is not None:
        bits.append(t["hi_lo"].format(hi=hi, lo=lo))
    if chance is not None:
        bits.append(t["chance"].format(chance=chance))
    no_data = t["no_data"]
    headline = _SEP[lang].join(bits) if bits else no_data

    rain = _rain_phrase(chance, lang)
    end = _END[lang]
    message = f"{headline}{_MID[lang]}{rain}".strip(end + " ")
    if headline != no_data:
        message += end

    detail = []
    if hi is not None:
        detail.append({"label": t["lbl_hi"], "value": f"{hi:g}°C"})
    if lo is not None:
        detail.append({"label": t["lbl_lo"], "value": f"{lo:g}°C"})
    if chance is not None:
        detail.append({"label": t["lbl_rain"], "value": f"{chance}%"})
    if day.get("uv") is not None:
        detail.append({"label": t["lbl_uv"], "value": f"{day['uv']:g}"})

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
    lang = _lang(question)
    t = _T[lang]
    if not forecast:
        return Answer(
            kind="forecast",
            location=location,
            title=location,
            message=t["no_forecast_now"],
        )

    target, label_key = _target_date(question, now)
    if target is not None:
        wanted = target.isoformat()
        match = next((d for d in forecast if d.get("date") == wanted), None)
        if match is None:
            horizon = forecast[-1].get("date")
            label = _DAY_LABELS[label_key][lang]
            return Answer(
                kind="forecast",
                location=location,
                title=f"{location} · {label}",
                message=t["horizon"].format(horizon=horizon, label=label),
            )
        return _format_day(location, match, label_key, now, lang)

    # No day named — a short outlook over the days we have.
    lines = []
    for day in forecast[:3]:
        key, fallback = _day_label_key(day.get("date", ""), now)
        lbl = _label_text(key, fallback, lang)
        chance = day.get("chance_of_rain")
        cond = day.get("condition_text") or ""
        piece = t["outlook_piece"].format(lbl=lbl, cond=cond)
        if chance is not None:
            piece += t["outlook_rain"].format(chance=chance)
        lines.append(piece)
    end = _END[lang]
    body = (_OUTLOOK_SEP[lang].join(lines) + end) if lines else (t["no_data"] + end)
    return Answer(
        kind="forecast",
        location=location,
        title=f"{location} · {t['outlook_title']}",
        message=body,
    )


# -- guidance: extractive retrieval over the official corpus ----------------

# Which hazard corpus a guidance question is about, by keyword. Deterministic
# routing rather than embedding similarity: the free lexical embedder does not
# bridge Chinese to the English corpus well, and a keyword map does.
_HAZARD_KEYWORDS: dict[str, tuple[str, ...]] = {
    "heat": ("高温", "中暑", "防暑", "太热", "酷热", "heat", "hot"),
    "uv": ("紫外线", "防晒", "晒伤", "日晒", "太阳", "uv", "sunscreen", "sun"),
    "wind": ("大风", "刮风", "阵风", "强风", "wind", "gale"),
    "rain": ("下雨", "雨天", "淋雨", "带伞", "积水", "洪水", "涉水", "蹚水",
             "暴雨", "rain", "flood", "umbrella"),
    "air_quality": ("空气", "雾霾", "口罩", "污染", "pm2", "air", "smog", "mask"),
}

# Labels for the answer title, by language — the corpus content stays in its
# original official English, which the citation attributes.
_HAZARD_LABEL = {
    "heat": {"zh": "高温 · 官方防护提示", "en": "Heat · official guidance",
             "ja": "高温 · 公式ガイダンス"},
    "uv": {"zh": "紫外线 · 官方防护提示", "en": "UV · official guidance",
           "ja": "紫外線 · 公式ガイダンス"},
    "wind": {"zh": "大风 · 官方安全提示", "en": "Wind · official safety guidance",
             "ja": "強風 · 公式安全ガイダンス"},
    "rain": {"zh": "降雨 · 官方安全提示", "en": "Rain · official safety guidance",
             "ja": "大雨 · 公式安全ガイダンス"},
    "air_quality": {"zh": "空气质量 · 官方提示", "en": "Air quality · official guidance",
                    "ja": "大気質 · 公式ガイダンス"},
}
_HAZARD_LABEL_FALLBACK = {"zh": "官方安全提示", "en": "Official safety guidance",
                          "ja": "公式安全ガイダンス"}

# English seed text per hazard. The free lexical retriever cannot match a
# Chinese question to an English section, so the seed carries the retrieval to
# the right passage and the question refines it — the same trick the RAG
# provider uses. Without it a Chinese query lands on whatever chunk shares the
# most generic tokens, usually the document's title block.
_HAZARD_SEED = {
    "heat": "extreme heat protective actions hydration outdoor activity clothing shade",
    "uv": "high uv index sun protection shade sunscreen protective clothing",
    "wind": "high wind safety outdoors indoors driving falling debris secure objects",
    "rain": "heavy rain flooding safety driving flooded roads walking on foot electrical",
    "air_quality": "air quality index limit outdoor exertion sensitive groups",
}

_retriever: Any = None
_retriever_lock = threading.RLock()
_retriever_tried = False


def _get_retriever() -> Any:
    """The local, zero-cost retriever, built once. None if the index is
    missing — the caller then abstains rather than fabricating."""
    global _retriever, _retriever_tried
    if _retriever is not None or _retriever_tried:
        return _retriever
    with _retriever_lock:
        if _retriever is None and not _retriever_tried:
            _retriever_tried = True
            try:
                from .advice import factory
                from .config import get_settings

                _retriever = factory.build_retriever(get_settings())
            except Exception:
                logger.exception("Assistant: could not build the guidance retriever.")
                _retriever = None
    return _retriever


def _hazards_from_question(question: str) -> list[str]:
    q = (question or "").lower()
    return [h for h, words in _HAZARD_KEYWORDS.items() if any(w in q for w in words)]


def _hazards_from_weather(weather: dict[str, Any]) -> list[str]:
    """When the question names no hazard ("出门要注意什么"), fall back to what
    the current weather actually warrants."""
    hazards = []
    uv = weather.get("uv")
    temp = weather.get("temp_c")
    wind = weather.get("wind_kph")
    rain = weather.get("precip_chance_next_hour")
    epa = weather.get("us_epa_index")
    if uv is not None and uv >= 6:
        hazards.append("uv")
    if temp is not None and temp >= 30:
        hazards.append("heat")
    if wind is not None and wind >= 30:
        hazards.append("wind")
    if rain is not None and rain >= 40:
        hazards.append("rain")
    if epa is not None and epa >= 3:
        hazards.append("air_quality")
    return hazards


def answer_guidance(location: str, question: str, weather: dict[str, Any]) -> Answer | None:
    """Retrieve the most relevant official guidance for a guidance question.

    Extractive, not generative: the answer is the retrieved passage itself,
    with its citation. No model, no external call. Returns None when nothing
    can be answered — the caller then states the scope honestly.
    """
    hazards = _hazards_from_question(question) or _hazards_from_weather(weather)
    if not hazards:
        return None

    retriever = _get_retriever()
    if retriever is None:
        return None

    from .advice.retrieval import RetrievalQuery

    seed = " ".join(_HAZARD_SEED.get(h, "") for h in hazards)
    try:
        results = retriever.retrieve(
            RetrievalQuery(
                text=f"{seed} {question}".strip(),
                hazard_types=tuple(hazards),
                jurisdiction="US",
                locale="en",
                top_k=4,
            )
        )
    except Exception:
        logger.exception("Assistant: guidance retrieval failed.")
        return None

    # The first chunk of each document is its title-plus-attribution block, not
    # guidance. Skip anything that is just provenance; keep the first real
    # passage.
    passage = next(
        (r.chunk for r in results if not r.chunk.content.lstrip().startswith("Source:")),
        None,
    )
    if passage is None:
        return None

    lang = _lang(question)
    hazard = passage.hazard_types[0] if passage.hazard_types else hazards[0]
    return Answer(
        kind="guidance",
        location=location,
        title=_HAZARD_LABEL.get(hazard, _HAZARD_LABEL_FALLBACK)[lang],
        message=passage.content,
        sources=[
            {
                "authority": passage.authority or passage.title,
                "source_url": passage.source_url,
                "title": passage.title,
            }
        ],
    )


def answer(snapshot_location: dict[str, Any], location_name: str, question: str,
           now: datetime | None = None) -> Answer:
    """Route and answer. Never raises for a well-formed call; an unanswerable
    question returns an honest ``unknown`` answer."""
    kind = classify(question)

    if kind == "forecast":
        return answer_forecast(
            location_name, snapshot_location.get("forecast") or [], question, now
        )

    t = _T[_lang(question)]
    if kind == "guidance":
        guided = answer_guidance(location_name, question, snapshot_location)
        if guided is not None:
            return guided
        # No matching official guidance — be honest about the current scope.
        return Answer(
            kind="unknown",
            location=location_name,
            title=location_name,
            message=t["guidance_na"],
        )

    return Answer(
        kind="unknown",
        location=location_name,
        title=location_name,
        message=t["unknown"],
    )
