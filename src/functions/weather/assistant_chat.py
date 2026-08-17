"""The paid chat layer for the assistant — natural answers, still grounded.

This is the enhancement over the deterministic assistant, not a replacement.
It is reached only when RAG is enabled, a chat client is configured, and the
cost guard grants the call. On *any* failure — over budget, throttled, a model
error, output that does not validate — the caller falls back to the free
deterministic answer. The paid path can only ever make the answer nicer, never
make it unavailable.

What keeps it honest is the same discipline as the advice card: the model is
handed only the weather data we actually have and the guidance we actually
retrieved, and its output is checked by deterministic code before it is shown —
every measurement it states must appear in the data, every citation must
resolve to a retrieved passage. Anything else falls back.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from . import assistant
from .advice import grounding

logger = logging.getLogger(__name__)

AVAILABLE_CITIES = ("Tokyo", "Osaka-Shi", "Sapporo")

# Numbers that must be grounded: those carrying a measurement unit. A bare
# integer ("未来 3 天", "6 小时内") is counting, not a fact to verify; a
# figure with a unit ("35°C", "降水 90%") is a claim about the weather and must
# match the data we supplied.
MEASUREMENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:°c|度|℃|%|km/h|kph|mm)", re.IGNORECASE)

SYSTEM_PROMPT = """你是一个天气看板的助手，只服务东京、大阪、札幌这三个城市。

规则，逐条遵守：
1. 只能使用 WEATHER_DATA 里给出的数字。绝不编造、估算或"记得"任何没给你的数值
   （气温、降水概率、紫外线等）。
2. 安全建议只能来自 RETRIEVED_GUIDANCE 的段落；用到哪段，就把它的 chunk_id 放进
   cited_chunk_ids。没有相关段落就不要给安全建议。
3. 越界的问题直接说明答不了，并说你能答什么，设 refused=true。越界包括：
   - 其他城市或地区（只有东京、大阪、札幌）
   - 过去的天气、超过 3 天的预报、超过约 6 小时的小时级细节
   - 没有的数据：下雪、官方预警、日出日落、花粉、能见度等；预报里也没有湿度/气压/空气质量
   - 非天气问题；地震、雷电等不在高温/紫外线/大风/降雨/空气质量五类里的安全话题
4. 诚实的精度：数据是城市级、约 15 分钟前的观测，预报按天。不要打包票"一定下/不下"，
   只说概率。安全指引来自美国官方，如需说明就如实标注。
5. 用自然、简短的中文回答，直接答被问的问题，不要罗列所有字段。

只返回一个 JSON 对象：
{"answer": "给用户看的中文回答", "cited_chunk_ids": ["用到的指引段落id"], "refused": false}"""


def _weather_facts(entry: dict[str, Any]) -> dict[str, Any]:
    """Only the served fields — this is the sole source of numbers the model
    may use."""
    current_keys = (
        "temp_c", "feelslike_c", "condition_text", "humidity", "wind_kph",
        "wind_dir", "pressure_mb", "uv", "pm2_5", "us_epa_index",
        "precip_chance_next_hour",
    )
    current = {k: entry[k] for k in current_keys if entry.get(k) is not None}
    forecast = [
        {
            k: day[k]
            for k in ("date", "chance_of_rain", "maxtemp_c", "mintemp_c",
                      "condition_text", "uv", "maxwind_kph")
            if day.get(k) is not None
        }
        for day in (entry.get("forecast") or [])
    ]
    return {"city": entry.get("name"), "current": current, "forecast": forecast}


def _allowed_numbers(facts: dict[str, Any], passages: list[Any]) -> set[str]:
    """Every figure the model was given, in the forms it might restate them."""
    values: set[str] = set()

    def add(v: Any) -> None:
        try:
            n = float(v)
        except (TypeError, ValueError):
            return
        values.add(f"{n:g}")
        values.add(f"{round(n):g}")
        values.add(f"{n:.1f}")

    for v in facts.get("current", {}).values():
        add(v)
    for day in facts.get("forecast", []):
        for v in day.values():
            add(v)
    # Numbers appearing in the cited guidance are also fair game.
    for p in passages:
        for n in grounding.NUMBER_RE.findall(p.content):
            values.add(n)
    return values


def _retrieve_guidance(question: str, entry: dict[str, Any]) -> list[Any]:
    """Passages the model may cite. Reuses the free local retriever; a failure
    just means no guidance is offered, not an error."""
    hazards = assistant._hazards_from_question(question) or assistant._hazards_from_weather(entry)
    if not hazards:
        return []
    retriever = assistant._get_retriever()
    if retriever is None:
        return []
    from .advice.retrieval import RetrievalQuery

    seed = " ".join(assistant._HAZARD_SEED.get(h, "") for h in hazards)
    try:
        results = retriever.retrieve(
            RetrievalQuery(
                text=f"{seed} {question}".strip(),
                hazard_types=tuple(hazards),
                jurisdiction="US",
                locale="en",
                top_k=3,
            )
        )
    except Exception:
        logger.exception("Assistant chat: guidance retrieval failed.")
        return []
    return [r.chunk for r in results if not r.chunk.content.lstrip().startswith("Source:")][:2]


def _build_user_prompt(question: str, facts: dict[str, Any], passages: list[Any]) -> str:
    guidance = [
        {"chunk_id": p.chunk_id, "authority": p.authority, "text": p.content[:700]}
        for p in passages
    ]
    return (
        "AVAILABLE_CITIES\n" + json.dumps(list(AVAILABLE_CITIES), ensure_ascii=False)
        + "\n\nWEATHER_DATA\n" + json.dumps(facts, ensure_ascii=False)
        + "\n\nRETRIEVED_GUIDANCE\n" + json.dumps(guidance, ensure_ascii=False)
        + "\n\nQUESTION\n" + question
    )


def _validate(answer: str, cited: list[str], allowed_numbers: set[str],
              retrieved_ids: set[str]) -> str:
    """Deterministic gate. Returns '' when the answer may be shown, else the
    first failure reason."""
    if not answer.strip():
        return "empty answer"
    for measurement in MEASUREMENT_RE.findall(answer):
        if measurement not in allowed_numbers:
            return f"figure '{measurement}' is not in the weather data"
    for cid in cited:
        if cid not in retrieved_ids:
            return f"citation {cid} was not retrieved"
    for pattern in grounding.BANNED_PATTERNS:
        if pattern.search(answer):
            return "banned pattern"
    return ""


def chat_answer(
    entry: dict[str, Any],
    city: str,
    question: str,
    settings: Any,
    guard: Any,
    session_id: str,
    chat_client: Any | None = None,
) -> assistant.Answer | None:
    """A natural, grounded answer — or None to fall back to the deterministic
    assistant. Never raises for a well-formed call."""
    from .advice import llm as llm_module
    from .advice.rag import estimate_cost

    client = chat_client or llm_module.get_chat_client(settings)
    if client is None:
        return None

    decision = guard.check(session_id, settings)
    if not decision.allowed:
        logger.info("Assistant chat: guard denied (%s); using the free answer.", decision.reason)
        return None

    passages = _retrieve_guidance(question, entry)
    facts = _weather_facts(entry)
    user_prompt = _build_user_prompt(question, facts, passages)

    try:
        response = client.complete(SYSTEM_PROMPT, user_prompt)
    except Exception:
        logger.exception("Assistant chat: model call failed.")
        return None

    # The call happened, so it is billed — record it before anything else.
    try:
        cost = estimate_cost(response.prompt_tokens, response.completion_tokens, settings.rag)
        guard.record(session_id, cost, settings)
    except Exception:
        logger.exception("Assistant chat: could not record spend.")

    try:
        payload = json.loads(response.text)
    except (TypeError, ValueError):
        logger.info("Assistant chat: output was not valid JSON; falling back.")
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("answer"), str):
        return None

    answer_text = payload["answer"].strip()
    raw_cited = payload.get("cited_chunk_ids")
    cited = [str(c) for c in raw_cited] if isinstance(raw_cited, list) else []

    reason = _validate(
        answer_text, cited,
        _allowed_numbers(facts, passages),
        {p.chunk_id for p in passages},
    )
    if reason:
        logger.info("Assistant chat: answer rejected (%s); falling back.", reason)
        return None

    cited_set = set(cited)
    sources = [
        {"authority": p.authority or p.title, "source_url": p.source_url, "title": p.title}
        for p in passages
        if p.chunk_id in cited_set
    ]
    return assistant.Answer(
        kind="refused" if payload.get("refused") else "chat",
        location=city,
        title="",
        message=answer_text,
        sources=sources,
    )
