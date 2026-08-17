"""The paid chat layer: natural answers when grounded, fallback when not.

A ScriptedChatClient stands in for the model, so these run with no Azure and
no spend. The recurring assertion is that the deterministic gate lets a
grounded answer through and turns anything ungrounded into a fallback (None).
"""

from __future__ import annotations

import json

import pytest

from weather import assistant_chat, assistant_guard
from weather.advice.llm import ScriptedChatClient
from weather.config import RagSettings, Settings


class FakeStore:
    def __init__(self, spent=0.0):
        self.spent = spent
        self.recorded = []

    def read_spend(self):
        return self.spent

    def add_spend(self, cost):
        self.spent += cost
        self.recorded.append(cost)


@pytest.fixture(autouse=True)
def reset_retriever():
    from weather import assistant

    assistant._retriever = None
    assistant._retriever_tried = False
    yield
    assistant._retriever = None
    assistant._retriever_tried = False


def settings():
    return Settings(rag=RagSettings(
        enabled=True, daily_budget_usd=10.0,
        input_cost_per_1k=0.0004, output_cost_per_1k=0.0016,
    ))


def guard(store=None):
    s = settings()
    return assistant_guard.AssistantGuard(s, store=store or FakeStore()), s


OSAKA = {
    "name": "Osaka-Shi", "temp_c": 29, "uv": 5, "wind_kph": 10,
    "precip_chance_next_hour": 5,
    "forecast": [{"date": "2026-08-18", "chance_of_rain": 70, "maxtemp_c": 29,
                  "mintemp_c": 24, "condition_text": "Light rain", "uv": 5}],
}


def scripted(payload):
    return ScriptedChatClient([json.dumps(payload, ensure_ascii=False)])


def test_a_grounded_answer_is_returned():
    g, s = guard()
    client = scripted({"answer": "后天大阪降水概率 70%，建议带伞。", "cited_chunk_ids": [], "refused": False})
    a = assistant_chat.chat_answer(OSAKA, "大阪", "后天该带伞吗", s, g, "s1", chat_client=client)
    assert a is not None and a.kind == "chat"
    assert "70%" in a.message


def test_an_invented_measurement_falls_back():
    g, s = guard()
    # data says 29°C; the model claims 45°C
    client = scripted({"answer": "后天最高 45°C，很热。", "cited_chunk_ids": [], "refused": False})
    a = assistant_chat.chat_answer(OSAKA, "大阪", "后天多热", s, g, "s2", chat_client=client)
    assert a is None  # → free fallback


def test_a_counting_number_is_allowed():
    g, s = guard()
    # "3 天" and "6 小时" are counting, not measurements → must not be rejected
    client = scripted({"answer": "未来 3 天大阪降水概率最高 70%。", "cited_chunk_ids": [], "refused": False})
    a = assistant_chat.chat_answer(OSAKA, "大阪", "这几天天气", s, g, "s3", chat_client=client)
    assert a is not None


def test_a_fabricated_citation_falls_back():
    g, s = guard()
    client = scripted({"answer": "记得带伞。", "cited_chunk_ids": ["not-a-real-chunk"], "refused": False})
    a = assistant_chat.chat_answer(OSAKA, "大阪", "下雨天注意什么", s, g, "s4", chat_client=client)
    assert a is None


def test_malformed_json_falls_back():
    g, s = guard()
    a = assistant_chat.chat_answer(OSAKA, "大阪", "后天呢", s, g, "s5",
                                   chat_client=ScriptedChatClient(["sorry not json"]))
    assert a is None


def test_over_budget_never_calls_the_model():
    store = FakeStore(spent=999.0)  # already over
    g, s = guard(store)
    client = scripted({"answer": "x", "cited_chunk_ids": [], "refused": False})
    a = assistant_chat.chat_answer(OSAKA, "大阪", "后天呢", s, g, "s6", chat_client=client)
    assert a is None
    assert client.calls == []  # the model was never invoked


def test_spend_is_recorded_after_a_call():
    store = FakeStore()
    g, s = guard(store)
    client = scripted({"answer": "后天降水 70%。", "cited_chunk_ids": [], "refused": False})
    assistant_chat.chat_answer(OSAKA, "大阪", "后天呢", s, g, "s7", chat_client=client)
    assert len(store.recorded) == 1 and store.recorded[0] > 0


def test_a_refusal_is_marked():
    g, s = guard()
    client = scripted({"answer": "我只有东京、大阪、札幌的数据。", "cited_chunk_ids": [], "refused": True})
    a = assistant_chat.chat_answer(OSAKA, "大阪", "京都天气", s, g, "s8", chat_client=client)
    assert a is not None and a.kind == "refused"
