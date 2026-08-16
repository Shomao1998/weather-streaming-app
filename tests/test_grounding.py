"""Output validation — the deterministic gate every generated card must pass.

No model grades anything here. These are the checks that decide whether copy
produced by a language model is allowed to reach a user, and each one is a
plain assertion about text, ids and numbers.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from weather.advice.grounding import (
    ADVICE_CODES,
    MAX_MESSAGE_CHARS,
    MAX_TITLE_CHARS,
    GeneratedAdvice,
    parse_model_output,
    validate,
)
from weather.advice.models import WeatherContext
from weather.advice.retrieval import RetrievedChunk


@pytest.fixture
def weather() -> WeatherContext:
    return WeatherContext(
        location="Tokyo",
        location_key="tokyo",
        observed_at_utc=datetime(2026, 8, 5, 6, 0, tzinfo=UTC),
        temp_c=36.4,
        feelslike_c=41.0,
        uv=9.0,
        wind_kph=12.0,
        precip_chance_next_hour=10,
        condition_text="Sunny",
    )


@pytest.fixture
def retrieved(knowledge_index) -> list[RetrievedChunk]:
    chunks = [c for c in knowledge_index.chunks if "heat" in c.hazard_types][:2]
    return [RetrievedChunk(chunk=c, score=0.5) for c in chunks]


def advice(**overrides) -> GeneratedAdvice:
    payload = {
        "title": "高温注意",
        "message": "多补水，避免正午外出。",
        "advice_codes": ("HYDRATE",),
        "supporting_chunk_ids": (),
    }
    payload.update(overrides)
    return GeneratedAdvice(**payload)


# -- parsing ----------------------------------------------------------------


def test_valid_json_parses():
    parsed = parse_model_output(
        json.dumps(
            {
                "title": "t",
                "message": "m",
                "advice_codes": ["HYDRATE"],
                "supporting_chunk_ids": ["a"],
            }
        )
    )
    assert parsed is not None
    assert parsed.advice_codes == ("HYDRATE",)


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        "```json\n{}\n```",  # fenced output is not repaired into shape
        '{"title": "t"}',
        '{"title": 1, "message": "m", "advice_codes": [], "supporting_chunk_ids": []}',
        '{"title": "t", "message": "m", "advice_codes": "HYDRATE", "supporting_chunk_ids": []}',
        "[]",
        "",
    ],
)
def test_malformed_output_is_rejected_not_repaired(raw):
    assert parse_model_output(raw) is None


def test_an_abstention_is_parsed_as_an_abstention():
    parsed = parse_model_output('{"abstain": true}')
    assert parsed is not None and parsed.abstained


# -- citations --------------------------------------------------------------


def test_a_card_citing_a_retrieved_chunk_passes(retrieved, weather):
    result = validate(
        advice(supporting_chunk_ids=(retrieved[0].chunk_id,)),
        retrieved=retrieved,
        weather=weather,
    )
    assert result.ok, result.failures


def test_a_fabricated_citation_is_rejected(retrieved, weather):
    result = validate(
        advice(supporting_chunk_ids=("nws-heat-during-999-deadbeefcafe",)),
        retrieved=retrieved,
        weather=weather,
    )
    assert not result.ok
    assert "not in this retrieval" in result.reason


def test_a_citation_from_a_different_request_is_rejected(retrieved, weather, knowledge_index):
    """A real chunk id that was not retrieved *this time* is still a fake
    citation: the copy cannot have been grounded in it."""
    other = next(c for c in knowledge_index.chunks if "wind" in c.hazard_types)
    result = validate(
        advice(supporting_chunk_ids=(other.chunk_id,)), retrieved=retrieved, weather=weather
    )
    assert not result.ok


def test_a_card_with_no_citation_is_rejected(retrieved, weather):
    result = validate(advice(supporting_chunk_ids=()), retrieved=retrieved, weather=weather)
    assert not result.ok
    assert "no supporting citations" in result.failures


def test_a_citation_that_became_disabled_is_rejected(retrieved, weather):
    from dataclasses import replace

    disabled = [
        RetrievedChunk(chunk=replace(retrieved[0].chunk, enabled=False), score=0.5),
        retrieved[1],
    ]
    result = validate(
        advice(supporting_chunk_ids=(disabled[0].chunk_id,)),
        retrieved=disabled,
        weather=weather,
    )
    assert not result.ok
    assert "disabled" in result.reason


# -- the closed action vocabulary -------------------------------------------


def test_an_invented_advice_code_is_rejected(retrieved, weather):
    result = validate(
        advice(advice_codes=("TAKE_IBUPROFEN",), supporting_chunk_ids=(retrieved[0].chunk_id,)),
        retrieved=retrieved,
        weather=weather,
    )
    assert not result.ok
    assert "unknown advice codes" in result.reason


@pytest.mark.parametrize("code", sorted(ADVICE_CODES))
def test_every_allowed_code_validates(code, retrieved, weather):
    result = validate(
        advice(advice_codes=(code,), supporting_chunk_ids=(retrieved[0].chunk_id,)),
        retrieved=retrieved,
        weather=weather,
    )
    assert result.ok, result.failures


# -- fabricated numbers -----------------------------------------------------


def test_a_number_that_matches_a_weather_fact_is_allowed(retrieved, weather):
    result = validate(
        advice(message="体感 41 度，注意补水。", supporting_chunk_ids=(retrieved[0].chunk_id,)),
        retrieved=retrieved,
        weather=weather,
    )
    assert result.ok, result.failures


def test_an_invented_measurement_is_rejected(retrieved, weather):
    """The failure mode that would make this feature untrustworthy."""
    result = validate(
        advice(message="今天最高将达 47 度。", supporting_chunk_ids=(retrieved[0].chunk_id,)),
        retrieved=retrieved,
        weather=weather,
    )
    assert not result.ok
    assert "47" in result.reason


def test_a_number_quoted_from_a_cited_source_is_allowed(retrieved, weather):
    # Extracted the way the validator extracts them. Substring matching would
    # accept "8" from a source that says "18", which is exactly the
    # fabrication the check exists to catch.
    from weather.advice.grounding import NUMBER_RE

    numbers = NUMBER_RE.findall(retrieved[0].chunk.content)
    number = numbers[0] if numbers else None
    if number is None:
        pytest.skip("no numeric guidance in this passage")
    result = validate(
        advice(message=f"参考官方建议：{number}。", supporting_chunk_ids=(retrieved[0].chunk_id,)),
        retrieved=retrieved,
        weather=weather,
    )
    assert result.ok, result.failures


# -- shape and tone ---------------------------------------------------------


def test_an_over_long_title_is_rejected(retrieved, weather):
    result = validate(
        advice(title="x" * (MAX_TITLE_CHARS + 1), supporting_chunk_ids=(retrieved[0].chunk_id,)),
        retrieved=retrieved,
        weather=weather,
    )
    assert not result.ok


def test_an_over_long_message_is_rejected(retrieved, weather):
    result = validate(
        advice(
            message="补水。" * (MAX_MESSAGE_CHARS // 2),
            supporting_chunk_ids=(retrieved[0].chunk_id,),
        ),
        retrieved=retrieved,
        weather=weather,
    )
    assert not result.ok


@pytest.mark.parametrize(
    "message",
    [
        "建议服药后再外出。",
        "气象台发布高温红色预警。",
        "**加粗**的建议。",
        "详见 [链接](https://example.com)。",
        "official warning issued for your area",
    ],
)
def test_medical_authority_and_markup_claims_are_rejected(message, retrieved, weather):
    result = validate(
        advice(message=message, supporting_chunk_ids=(retrieved[0].chunk_id,)),
        retrieved=retrieved,
        weather=weather,
    )
    assert not result.ok


def test_an_abstention_never_validates(retrieved, weather):
    assert not validate(
        GeneratedAdvice("", "", (), (), abstained=True), retrieved=retrieved, weather=weather
    ).ok


def test_all_failures_are_collected_not_just_the_first(retrieved, weather):
    """The telemetry logs every reason, so a bad prompt is diagnosable in one
    look rather than one deploy at a time."""
    result = validate(
        advice(title="", advice_codes=("NOPE",), supporting_chunk_ids=()),
        retrieved=retrieved,
        weather=weather,
    )
    assert len(result.failures) >= 3
