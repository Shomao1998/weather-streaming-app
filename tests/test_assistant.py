"""The deterministic assistant: intent routing and forecast answers.

No model, so every answer is a pure function of the question and the served
forecast — which is exactly what these tests pin down.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import azure.functions as func
import pytest

from weather import assistant

NOW = datetime(2026, 8, 16, 6, 0, tzinfo=UTC)

OSAKA_FORECAST = [
    {"date": "2026-08-16", "chance_of_rain": 10, "maxtemp_c": 31, "mintemp_c": 25,
     "condition_text": "Partly cloudy", "uv": 6},
    {"date": "2026-08-17", "chance_of_rain": 70, "maxtemp_c": 29, "mintemp_c": 24,
     "condition_text": "Light rain", "uv": 5},
    {"date": "2026-08-18", "chance_of_rain": 20, "maxtemp_c": 32, "mintemp_c": 26,
     "condition_text": "Sunny", "uv": 7},
]


# -- intent routing ---------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    ["明天会下雨吗", "今天几度", "后天热吗", "大阪天气怎么样", "会不会下雪", "how hot tomorrow"],
)
def test_forecast_questions_route_to_forecast(question):
    assert assistant.classify(question) == "forecast"


@pytest.mark.parametrize(
    "question",
    ["下雨天要注意什么", "该带什么防护", "紫外线强怎么办", "出门需要准备什么", "how to protect"],
)
def test_guidance_questions_route_to_guidance(question):
    assert assistant.classify(question) == "guidance"


def test_guidance_wins_over_forecast_when_both_present():
    # "下雨天注意什么" contains a forecast word (下雨) but is asking for guidance.
    assert assistant.classify("下雨天要注意什么") == "guidance"


@pytest.mark.parametrize("question", ["股票会涨吗", "你叫什么名字", "1+1"])
def test_off_topic_is_unknown(question):
    assert assistant.classify(question) == "unknown"


# -- forecast answers -------------------------------------------------------


def test_tomorrow_rain_answered_from_data():
    a = assistant.answer_forecast("大阪", OSAKA_FORECAST, "明天会下雨吗", now=NOW)
    assert a.kind == "forecast"
    assert a.title == "大阪 · 明天"
    assert "70%" in a.message
    assert "带伞" in a.message  # a 70% chance produces an umbrella nudge
    assert {"label": "降水概率", "value": "70%"} in a.detail


def test_today_and_day_after_pick_the_right_row():
    today = assistant.answer_forecast("大阪", OSAKA_FORECAST, "今天几度", now=NOW)
    assert today.title == "大阪 · 今天" and "31°C" in today.message
    after = assistant.answer_forecast("大阪", OSAKA_FORECAST, "后天呢", now=NOW)
    assert after.title == "大阪 · 后天" and "Sunny" in after.message


def test_low_chance_says_basically_no_rain():
    a = assistant.answer_forecast("大阪", OSAKA_FORECAST, "今天会下雨吗", now=NOW)
    assert "基本不会下雨" in a.message


def test_no_day_named_gives_a_multi_day_outlook():
    a = assistant.answer_forecast("大阪", OSAKA_FORECAST, "大阪天气怎么样", now=NOW)
    assert "今天" in a.message and "明天" in a.message and "后天" in a.message


def test_a_day_past_the_horizon_is_honest():
    short = OSAKA_FORECAST[:1]  # only today
    a = assistant.answer_forecast("大阪", short, "明天会下雨吗", now=NOW)
    assert "还看不到" in a.message or "预报只到" in a.message


def test_empty_forecast_does_not_fabricate():
    a = assistant.answer_forecast("大阪", [], "明天会下雨吗", now=NOW)
    assert a.kind == "forecast"
    assert "没有" in a.message or "稍后" in a.message


# -- routing through answer() ----------------------------------------------


def test_answer_routes_forecast():
    entry = {"name": "Osaka", "forecast": OSAKA_FORECAST}
    a = assistant.answer(entry, "大阪", "明天会下雨吗", now=NOW)
    assert a.kind == "forecast" and "70%" in a.message


def test_answer_routes_guidance_to_a_cited_passage():
    entry = {"name": "Osaka", "forecast": OSAKA_FORECAST}
    a = assistant.answer(entry, "大阪", "下雨天注意什么", now=NOW)
    assert a.kind == "guidance"
    assert a.sources and a.sources[0]["source_url"].startswith("https://")


def test_answer_unknown_states_scope():
    entry = {"name": "Osaka", "forecast": OSAKA_FORECAST}
    a = assistant.answer(entry, "大阪", "股票会涨吗", now=NOW)
    assert a.kind == "unknown" and "预报" in a.message


# -- the HTTP surface -------------------------------------------------------


@pytest.fixture(autouse=True)
def storage_env(monkeypatch):
    monkeypatch.setenv("WEATHER_API_KEY", "local-test-key")
    monkeypatch.setenv("EVENT_HUB_ENABLED", "false")
    monkeypatch.setenv("STORAGE_ENABLED", "true")
    monkeypatch.setenv("STORAGE_CONNECTION_STRING", "UseDevelopmentStorage=true")
    from weather.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def app_module():
    import function_app

    return function_app


def get(**params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return func.HttpRequest(
        method="GET", url=f"http://localhost/api/ask?{query}", params=params, headers={}, body=b""
    )


def snapshot():
    # The endpoint uses the real clock, so build dates relative to real today
    # with a wet *tomorrow* — otherwise the assertion drifts with the calendar.
    from datetime import timedelta

    today = datetime.now(UTC).date()
    forecast = [
        {"date": (today + timedelta(days=n)).isoformat(),
         "chance_of_rain": 70 if n == 1 else 15,
         "maxtemp_c": 30, "mintemp_c": 24,
         "condition_text": "Light rain" if n == 1 else "Sunny", "uv": 6}
        for n in range(3)
    ]
    return {"generated_at_utc": NOW.isoformat(), "locations": [
        {"location_key": "34.69,135.50", "name": "Osaka", "forecast": forecast},
    ]}


def test_ask_endpoint_answers_a_forecast_question(app_module, monkeypatch):
    monkeypatch.setattr(app_module.serving, "read_serving_document", lambda *a, **k: snapshot())
    resp = app_module.api_ask(get(location="Osaka", q="明天会下雨吗"))
    assert resp.status_code == 200
    body = json.loads(resp.get_body())
    assert body["kind"] == "forecast"
    assert "70%" in body["message"]


def test_ask_endpoint_requires_a_question(app_module):
    resp = app_module.api_ask(get(location="Osaka"))
    assert resp.status_code == 400


def test_ask_endpoint_rejects_unknown_location(app_module, monkeypatch):
    monkeypatch.setattr(app_module.serving, "read_serving_document", lambda *a, **k: snapshot())
    resp = app_module.api_ask(get(location="Kyoto", q="明天会下雨吗"))
    assert resp.status_code == 400


def test_ask_endpoint_survives_missing_data(app_module, monkeypatch):
    from weather import serving as serving_mod

    def raise_unavailable(*a, **k):
        raise serving_mod.ServingDataUnavailable("no data")

    monkeypatch.setattr(app_module.serving, "read_serving_document", raise_unavailable)
    resp = app_module.api_ask(get(location="Osaka", q="明天会下雨吗"))
    assert resp.status_code == 503
    assert json.loads(resp.get_body())["kind"] == "unknown"


# -- guidance retrieval (Part 2) --------------------------------------------


@pytest.fixture(autouse=True)
def reset_assistant_retriever():
    """The retriever is cached in the module; reset it so a test's settings
    (not an earlier test's) build it."""
    assistant._retriever = None
    assistant._retriever_tried = False
    yield
    assistant._retriever = None
    assistant._retriever_tried = False


@pytest.mark.parametrize(
    "question",
    ["下雨天开车要注意什么", "能不能蹚水过去", "紫外线强怎么防护", "大风天要小心什么", "出门该带什么"],
)
def test_hazard_questions_route_to_guidance(question):
    assert assistant.classify(question) == "guidance"


def test_a_hazard_only_question_still_routes_to_guidance():
    # No generic guidance verb, but "蹚水" names a flood situation.
    assert assistant.classify("能不能蹚水过去") == "guidance"


HOT_WEATHER = {"name": "Osaka-Shi", "temp_c": 36, "uv": 8, "wind_kph": 10,
               "precip_chance_next_hour": 5, "forecast": []}


@pytest.mark.parametrize(
    ("question", "expect_authority_fragment"),
    [
        ("下雨天开车要注意什么", "NOAA"),
        ("紫外线强怎么防护", "Environmental Protection"),
        ("大风天要小心什么", "NOAA"),
    ],
)
def test_guidance_returns_official_passage_and_citation(question, expect_authority_fragment):
    a = assistant.answer(HOT_WEATHER, "大阪", question)
    assert a.kind == "guidance"
    assert a.message and not a.message.lstrip().startswith("Source:")  # not the boilerplate
    assert a.sources and expect_authority_fragment in a.sources[0]["authority"]
    assert a.sources[0]["source_url"].startswith("https://")


def test_guidance_skips_the_attribution_block():
    a = assistant.answer(HOT_WEATHER, "大阪", "下雨天要注意什么")
    # The message is real guidance, not "Source: ... public domain".
    assert "public domain" not in a.message


def test_generic_question_uses_the_weather_to_pick_a_hazard():
    # No hazard named; UV is 8, so sun protection is the relevant guidance.
    a = assistant.answer(HOT_WEATHER, "大阪", "出门要注意什么")
    assert a.kind == "guidance"
    assert "紫外线" in a.title or "shade" in a.message.lower() or "sunscreen" in a.message.lower()


def test_calm_weather_generic_question_is_honest():
    calm = {"name": "Sapporo", "temp_c": 18, "uv": 2, "wind_kph": 8,
            "precip_chance_next_hour": 0, "us_epa_index": 1, "forecast": []}
    a = assistant.answer(calm, "札幌", "出门要注意什么")
    # Nothing warrants a hazard warning and none is named → honest scope note.
    assert a.kind == "unknown"


def test_ask_endpoint_answers_guidance_with_a_citation(app_module, monkeypatch):
    snap = {"generated_at_utc": NOW.isoformat(), "locations": [
        {"location_key": "34.69,135.50", "name": "Osaka", "temp_c": 36, "uv": 8,
         "wind_kph": 10, "precip_chance_next_hour": 5, "forecast": []},
    ]}
    monkeypatch.setattr(app_module.serving, "read_serving_document", lambda *a, **k: snap)
    resp = app_module.api_ask(get(location="Osaka", q="紫外线强怎么防护"))
    assert resp.status_code == 200
    body = json.loads(resp.get_body())
    assert body["kind"] == "guidance"
    assert body["sources"][0]["source_url"].startswith("https://")
