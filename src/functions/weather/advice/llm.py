"""The model call, behind a protocol, with the prompt it is given.

Timeouts and a single retry are enforced here; there is no layered retry
anywhere above this, because two components each retrying three times is how a
slow dependency turns into an outage.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol

from .models import AdviceTrigger, WeatherContext
from .retrieval import RetrievedChunk

logger = logging.getLogger(__name__)

# Bump when the prompt changes. It is part of the generation cache key, so a
# reworded instruction invalidates cached answers instead of silently serving
# output the new prompt would not have produced.
PROMPT_VERSION = "advice-2026-08-05.1"

MAX_CHUNK_CHARS = 700


class LlmError(RuntimeError):
    """Any model failure. The caller falls back to the template."""


class LlmTimeout(LlmError):
    pass


@dataclass(frozen=True)
class LlmResponse:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    latency_ms: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class ChatClient(Protocol):
    name: str
    model: str

    def complete(self, system: str, user: str) -> LlmResponse: ...


SYSTEM_PROMPT = """You write one short weather-safety card for a dashboard.

You are given four blocks: WEATHER_FACTS, TRIGGER, RETRIEVED_GUIDANCE and
OUTPUT_SCHEMA. Follow this policy exactly.

POLICY
1. WEATHER_FACTS is the only source of current conditions. Never state a
   weather condition, measurement or forecast that is not in that block.
2. RETRIEVED_GUIDANCE is the only source of recommended actions. Never
   recommend an action that is not supported by one of those passages.
3. Every card must cite at least one chunk id from RETRIEVED_GUIDANCE in
   supporting_chunk_ids. Cite only ids that appear in that block.
4. Never assert or change an official alert level, and never claim an
   authority has issued a warning.
5. Never give individual medical, legal or financial instruction. The
   guidance is general public safety information.
6. Do not restate numbers that are not in WEATHER_FACTS or RETRIEVED_GUIDANCE.
7. If the retrieved guidance does not support useful advice for these
   conditions, return {"abstain": true} and nothing else.
8. Write for a card: a title of at most 12 characters and a message of at
   most 60 characters, in the same language as the TRIGGER block's language
   field. Plain text only — no Markdown, no emoji, no fields outside the
   schema.

Return one JSON object and nothing else."""


def _trim(text: str, limit: int = MAX_CHUNK_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def build_user_prompt(
    trigger: AdviceTrigger,
    weather: WeatherContext,
    retrieved: list[RetrievedChunk],
    question: str | None = None,
    language: str = "zh",
) -> str:
    """Assemble the four blocks.

    Only the facts a rule actually used are included. Pasting the whole
    weather record and the whole corpus into the prompt would cost more, dilute
    attention, and give the model material to speculate from.
    """
    facts: dict[str, Any] = {"location": weather.location}
    for key, value in (
        ("temperature_c", weather.temp_c),
        ("feels_like_c", weather.feelslike_c),
        ("uv_index", weather.uv),
        ("wind_kph", weather.wind_kph),
        ("precip_chance_next_hour_percent", weather.precip_chance_next_hour),
        ("condition", weather.condition_text),
    ):
        if value is not None:
            facts[key] = value
    if weather.observed_at_utc:
        facts["observed_at_utc"] = weather.observed_at_utc.isoformat().replace("+00:00", "Z")

    guidance = [
        {
            "chunk_id": item.chunk_id,
            "authority": item.chunk.authority,
            "title": item.chunk.title,
            "heading": item.chunk.heading,
            "text": _trim(item.chunk.content),
        }
        for item in retrieved
    ]

    trigger_block: dict[str, Any] = {"trigger_code": str(trigger), "language": language}
    if question:
        trigger_block["user_question"] = _trim(question, 200)

    schema = {
        "title": "string",
        "message": "string",
        "advice_codes": ["ALLOWED_CODE"],
        "supporting_chunk_ids": ["chunk id from RETRIEVED_GUIDANCE"],
    }

    return (
        "WEATHER_FACTS\n"
        + json.dumps(facts, ensure_ascii=False)
        + "\n\nTRIGGER\n"
        + json.dumps(trigger_block, ensure_ascii=False)
        + "\n\nRETRIEVED_GUIDANCE\n"
        + json.dumps(guidance, ensure_ascii=False)
        + "\n\nOUTPUT_SCHEMA\n"
        + json.dumps(schema, ensure_ascii=False)
    )


class AzureOpenAIChatClient:
    """Azure OpenAI chat completions, by managed identity, with JSON mode."""

    def __init__(
        self,
        endpoint: str,
        deployment: str,
        api_version: str = "2024-10-21",
        timeout_seconds: float = 8.0,
        max_output_tokens: int = 300,
        temperature: float = 0.2,
        credential: Any | None = None,
    ) -> None:
        self.name = "azure-openai"
        self.model = deployment
        self._endpoint = endpoint.rstrip("/")
        self._api_version = api_version
        self._timeout = timeout_seconds
        self._max_output_tokens = max_output_tokens
        self._temperature = temperature
        self._credential = credential
        self._client: Any = None
        self._lock = threading.RLock()

    def _ensure_client(self) -> Any:
        with self._lock:
            if self._client is None:
                from azure.identity import get_bearer_token_provider
                from openai import AzureOpenAI

                from .. import clients as azure_clients

                credential = self._credential or azure_clients.get_credential()
                self._client = AzureOpenAI(
                    azure_endpoint=self._endpoint,
                    api_version=self._api_version,
                    azure_ad_token_provider=get_bearer_token_provider(
                        credential, "https://cognitiveservices.azure.com/.default"
                    ),
                    timeout=self._timeout,
                    # One retry inside the SDK, none above it.
                    max_retries=1,
                )
            return self._client

    def complete(self, system: str, user: str) -> LlmResponse:
        started = time.monotonic()
        try:
            response = self._ensure_client().chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=self._temperature,
                max_tokens=self._max_output_tokens,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            elapsed = (time.monotonic() - started) * 1000
            if "timeout" in str(exc).lower():
                raise LlmTimeout(f"model timed out after {elapsed:.0f}ms") from exc
            raise LlmError(str(exc)) from exc

        usage = getattr(response, "usage", None)
        return LlmResponse(
            text=response.choices[0].message.content or "",
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            model=self.model,
            latency_ms=(time.monotonic() - started) * 1000,
        )


class ScriptedChatClient:
    """A deterministic stand-in used by tests and offline evals.

    It composes an answer from the retrieved chunks rather than pretending to
    be a language model: the point is to exercise parsing, validation, caching
    and fallback without a network call, not to simulate generation quality.
    Eval runs record which client produced them so the two are never confused.
    """

    name = "scripted"

    def __init__(self, responses: list[str] | None = None, model: str = "scripted-v1") -> None:
        self.model = model
        self._responses = list(responses or [])
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> LlmResponse:
        self.calls.append((system, user))
        if self._responses:
            text = self._responses.pop(0)
            # Rough token estimates so cost telemetry is exercised offline
            # rather than always reading zero.
            return LlmResponse(
                text=text,
                prompt_tokens=len(user) // 4,
                completion_tokens=len(text) // 4,
                model=self.model,
                latency_ms=1.0,
            )

        payload = json.loads(user.split("RETRIEVED_GUIDANCE\n", 1)[1].split("\n\nOUTPUT_SCHEMA")[0])
        first = payload[0] if payload else None
        if first is None:
            return LlmResponse(text=json.dumps({"abstain": True}), model=self.model)
        return LlmResponse(
            text=json.dumps(
                {
                    "title": "天气提醒",
                    "message": "请参考官方安全建议，注意出行安全。",
                    "advice_codes": ["ALLOW_EXTRA_TRAVEL_TIME"],
                    "supporting_chunk_ids": [first["chunk_id"]],
                },
                ensure_ascii=False,
            ),
            prompt_tokens=len(user) // 4,
            completion_tokens=40,
            model=self.model,
            latency_ms=1.0,
        )


def get_chat_client(settings: Any | None = None) -> ChatClient | None:
    """The configured client, or None when Azure OpenAI is not set up.

    Returning None rather than raising is deliberate: an unconfigured
    deployment is a normal state for this project, and it must degrade to the
    template provider rather than to an error.
    """
    rag = getattr(settings, "rag", None) if settings else None
    if rag and rag.openai_endpoint and rag.chat_deployment:
        return AzureOpenAIChatClient(
            endpoint=rag.openai_endpoint,
            deployment=rag.chat_deployment,
            timeout_seconds=rag.request_timeout_seconds,
            max_output_tokens=rag.max_output_tokens,
        )
    return None
