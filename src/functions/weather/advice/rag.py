"""`RagAdviceProvider`: retrieval-grounded wording, behind the phase-one seam.

What this does **not** do is as important as what it does. It never decides
whether to show a card, what the risk is, how severe it is, whether the weather
is fresh, or when to suppress — all of that stayed in the rule engine and the
frequency policy. This class only chooses words, and it must cite the passage
each recommendation came from.

Any failure — no usable chunks, a timeout, malformed output, a citation that
does not resolve — returns to `TemplateAdviceProvider`. A deterministic card is
always better than no card, and always better than an ungrounded one.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings, get_settings
from . import grounding
from . import llm as llm_module
from .cache import TtlCache, generation_key
from .knowledge import (
    HAZARD_AIR_QUALITY,
    HAZARD_HEAT,
    HAZARD_RAIN,
    HAZARD_UV,
    HAZARD_WIND,
)
from .models import AdviceContent, AdviceTrigger, Source, WeatherContext
from .providers import TemplateAdviceProvider
from .retrieval import AdviceRetriever, RetrievalError, RetrievalQuery, RetrievedChunk

logger = logging.getLogger(__name__)

# A trigger maps to hazards deterministically. The model never chooses what to
# search for: a UV card must not be able to ground itself in flood guidance.
TRIGGER_HAZARDS: dict[AdviceTrigger, tuple[str, ...]] = {
    AdviceTrigger.EXTREME_HEAT: (HAZARD_HEAT,),
    AdviceTrigger.HIGH_UV: (HAZARD_UV,),
    AdviceTrigger.HIGH_WIND: (HAZARD_WIND,),
    AdviceTrigger.RAIN_EXPECTED: (HAZARD_RAIN,),
}

# Seed query text per trigger. Combined with the live values and, for a user
# question, the question itself — never the question alone, which would let a
# user steer retrieval away from the hazard that actually applies.
TRIGGER_QUERIES: dict[AdviceTrigger, str] = {
    AdviceTrigger.EXTREME_HEAT: "extreme heat protective actions hydration outdoor activity",
    AdviceTrigger.HIGH_UV: "high uv index sun protection shade sunscreen clothing",
    AdviceTrigger.HIGH_WIND: "high wind safety outdoors driving falling debris",
    AdviceTrigger.RAIN_EXPECTED: "heavy rain safety umbrella travel flooded roads",
}

ALL_HAZARDS = (HAZARD_HEAT, HAZARD_UV, HAZARD_WIND, HAZARD_RAIN, HAZARD_AIR_QUALITY)


@dataclass
class RagTelemetry:
    """Per-request measurements, logged as one structured line."""

    trigger: str = ""
    retrieval_query_hash: str = ""
    metadata_filters: dict[str, Any] = field(default_factory=dict)
    retrieved_chunk_ids: list[str] = field(default_factory=list)
    retrieval_scores: list[float] = field(default_factory=list)
    index_version: str = ""
    prompt_version: str = llm_module.PROMPT_VERSION
    model: str = ""
    generation_method: str = ""
    validation: str = ""
    fallback_reason: str = ""
    retrieval_ms: float = 0.0
    llm_ms: float = 0.0
    total_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    retrieval_cache_hit: bool = False
    generation_cache_hit: bool = False
    estimated_cost_usd: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "trigger": self.trigger,
            "retrieval_query_hash": self.retrieval_query_hash,
            "metadata_filters": self.metadata_filters,
            "retrieved_chunk_ids": self.retrieved_chunk_ids,
            "retrieval_scores": [round(s, 4) for s in self.retrieval_scores],
            "index_version": self.index_version,
            "prompt_version": self.prompt_version,
            "model": self.model,
            "generation_method": self.generation_method,
            "validation": self.validation,
            "retrieval_ms": round(self.retrieval_ms, 1),
            "llm_ms": round(self.llm_ms, 1),
            "total_ms": round(self.total_ms, 1),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "retrieval_cache_hit": self.retrieval_cache_hit,
            "generation_cache_hit": self.generation_cache_hit,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
        }
        if self.fallback_reason:
            payload["fallback_reason"] = self.fallback_reason
        return payload


def estimate_cost(prompt_tokens: int, completion_tokens: int, settings: Any) -> float:
    return (
        prompt_tokens / 1000 * settings.input_cost_per_1k
        + completion_tokens / 1000 * settings.output_cost_per_1k
    )


class RagAdviceProvider:
    """Implements the phase-one `AdviceContentProvider` protocol."""

    name = "rag-v1"

    def __init__(
        self,
        retriever: AdviceRetriever,
        chat_client: llm_module.ChatClient | None = None,
        settings: Settings | None = None,
        fallback: TemplateAdviceProvider | None = None,
    ) -> None:
        self._retriever = retriever
        self._chat = chat_client
        self._settings = settings or get_settings()
        self._fallback = fallback or TemplateAdviceProvider()
        rag = self._settings.rag
        self._retrieval_cache: TtlCache[list[RetrievedChunk]] = TtlCache(
            max_entries=rag.retrieval_cache_entries, ttl_seconds=rag.retrieval_cache_ttl_seconds
        )
        self._generation_cache: TtlCache[AdviceContent] = TtlCache(
            max_entries=rag.generation_cache_entries,
            ttl_seconds=rag.generation_cache_ttl_seconds,
        )
        self.last_telemetry: RagTelemetry | None = None

    # -- query construction -------------------------------------------------

    def build_query(
        self, trigger: AdviceTrigger, weather: WeatherContext, question: str | None = None
    ) -> RetrievalQuery:
        rag = self._settings.rag
        hazards = TRIGGER_HAZARDS.get(trigger, ALL_HAZARDS)
        parts = [TRIGGER_QUERIES.get(trigger, "weather safety guidance")]

        # Live values go into the query text so that, for example, an extreme
        # UV reading pulls the "8 and above" passage rather than the general one.
        if trigger == AdviceTrigger.HIGH_UV and weather.uv is not None:
            parts.append(f"uv index {weather.uv:g}")
        if trigger == AdviceTrigger.EXTREME_HEAT and weather.temp_c is not None:
            parts.append(f"temperature {weather.temp_c:g} celsius")
        if trigger == AdviceTrigger.HIGH_WIND and weather.wind_kph is not None:
            parts.append(f"wind {weather.wind_kph:g} kph")
        if trigger == AdviceTrigger.RAIN_EXPECTED and weather.precip_chance_next_hour is not None:
            parts.append(f"rain probability {weather.precip_chance_next_hour}%")
        if question:
            # Appended, never substituted: the hazard filter and the trigger
            # seed still decide what corpus is in scope.
            parts.append(question.strip()[:200])

        return RetrievalQuery(
            text=" ".join(parts),
            hazard_types=hazards,
            jurisdiction=rag.jurisdiction,
            locale=rag.locale,
            top_k=rag.top_k,
        )

    # -- retrieval ----------------------------------------------------------

    def _retrieve(self, query: RetrievalQuery, telemetry: RagTelemetry) -> list[RetrievedChunk]:
        key = query.cache_key(self._retriever.index_version)
        telemetry.retrieval_query_hash = key
        telemetry.metadata_filters = query.filter_description()

        cached = self._retrieval_cache.get(key)
        if cached is not None:
            telemetry.retrieval_cache_hit = True
            return cached

        started = time.monotonic()
        chunks = self._retriever.retrieve(query)
        telemetry.retrieval_ms = (time.monotonic() - started) * 1000

        # Re-assert the filters the retriever was asked to apply. A local index
        # can go stale between load and query, and a service-side filter is a
        # promise rather than a guarantee this code made itself.
        usable = [
            c
            for c in chunks
            if c.chunk.is_effective()
            and (not query.hazard_types or set(c.chunk.hazard_types) & set(query.hazard_types))
        ]
        self._retrieval_cache.put(key, usable)
        return usable

    # -- generation ---------------------------------------------------------

    def generate(
        self,
        trigger: AdviceTrigger,
        weather: WeatherContext,
        question: str | None = None,
    ) -> AdviceContent:
        started = time.monotonic()
        rag = self._settings.rag
        telemetry = RagTelemetry(trigger=str(trigger), index_version=self._retriever.index_version)
        self.last_telemetry = telemetry

        def fall_back(reason: str) -> AdviceContent:
            telemetry.fallback_reason = reason
            telemetry.generation_method = self._fallback.name
            telemetry.total_ms = (time.monotonic() - started) * 1000
            logger.info(
                "ADVICE_RAG_FALLBACK %s",
                telemetry.as_dict(),
                extra={"custom_dimensions": telemetry.as_dict()},
            )
            return self._fallback.generate(trigger, weather)

        if not rag.enabled:
            return fall_back("rag disabled")
        if self._chat is None:
            return fall_back("no chat client configured")

        try:
            query = self.build_query(trigger, weather, question)
            retrieved = self._retrieve(query, telemetry)
        except RetrievalError as exc:
            return fall_back(f"retrieval failed: {exc}")
        except Exception as exc:
            logger.exception("Retrieval raised unexpectedly.")
            return fall_back(f"retrieval error: {exc}")

        telemetry.retrieved_chunk_ids = [c.chunk_id for c in retrieved]
        telemetry.retrieval_scores = [c.score for c in retrieved]

        if len(retrieved) < rag.min_chunks:
            # Too little evidence is a reason to say the safe thing, not to
            # let the model improvise.
            return fall_back(f"only {len(retrieved)} usable chunks, need {rag.min_chunks}")

        cache_key = generation_key(
            weather_snapshot_id=weather.snapshot_id,
            trigger=str(trigger),
            chunk_ids=telemetry.retrieved_chunk_ids,
            prompt_version=llm_module.PROMPT_VERSION,
            model=self._chat.model,
            index_version=self._retriever.index_version,
            question=question,
        )
        cached = self._generation_cache.get(cache_key)
        if cached is not None:
            telemetry.generation_cache_hit = True
            telemetry.generation_method = cached.generation_method
            telemetry.validation = "cached"
            telemetry.total_ms = (time.monotonic() - started) * 1000
            logger.info(
                "ADVICE_RAG_CACHED %s",
                telemetry.as_dict(),
                extra={"custom_dimensions": telemetry.as_dict()},
            )
            return cached

        user_prompt = llm_module.build_user_prompt(
            trigger, weather, retrieved, question=question, language=rag.language
        )
        try:
            response = self._chat.complete(llm_module.SYSTEM_PROMPT, user_prompt)
        except llm_module.LlmTimeout as exc:
            return fall_back(f"model timeout: {exc}")
        except llm_module.LlmError as exc:
            return fall_back(f"model error: {exc}")
        except Exception as exc:
            logger.exception("Chat client raised unexpectedly.")
            return fall_back(f"model error: {exc}")

        telemetry.llm_ms = response.latency_ms
        telemetry.model = response.model or self._chat.model
        telemetry.prompt_tokens = response.prompt_tokens
        telemetry.completion_tokens = response.completion_tokens
        telemetry.estimated_cost_usd = estimate_cost(
            response.prompt_tokens, response.completion_tokens, rag
        )

        parsed = grounding.parse_model_output(response.text)
        if parsed is None:
            return fall_back("model output was not valid structured JSON")

        validation = grounding.validate(parsed, retrieved=retrieved, weather=weather)
        telemetry.validation = "ok" if validation.ok else "; ".join(validation.failures)
        if not validation.ok:
            return fall_back(f"validation failed: {validation.reason}")

        cited = [c for c in retrieved if c.chunk_id in set(parsed.supporting_chunk_ids)]
        content = AdviceContent(
            title=parsed.title,
            message=parsed.message,
            # Evidence still comes from the rule, attached by `content_for`.
            evidence=(),
            generation_method=self.name,
            sources=tuple(
                Source(
                    chunk_id=item.chunk_id,
                    source_document_id=item.chunk.source_document_id,
                    authority=item.chunk.authority,
                    title=item.chunk.title,
                    source_url=item.chunk.source_url,
                )
                for item in cited
            ),
            advice_codes=parsed.advice_codes,
        )

        self._generation_cache.put(cache_key, content)
        telemetry.generation_method = self.name
        telemetry.total_ms = (time.monotonic() - started) * 1000
        logger.info(
            "ADVICE_RAG_GENERATED %s",
            telemetry.as_dict(),
            extra={"custom_dimensions": telemetry.as_dict()},
        )
        return content

    # -- introspection ------------------------------------------------------

    def cache_stats(self) -> dict[str, Any]:
        return {
            "retrieval": self._retrieval_cache.stats.as_dict(),
            "generation": self._generation_cache.stats.as_dict(),
        }

    def clear_caches(self) -> None:
        self._retrieval_cache.clear()
        self._generation_cache.clear()
