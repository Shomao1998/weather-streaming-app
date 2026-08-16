"""Service orchestration: freshness, deduplication, frequency, mute, escalation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from weather.advice import (
    AdviceContent,
    AdviceOutcome,
    AdviceService,
    AdviceTrigger,
    InvalidLocation,
    TemplateAdviceProvider,
)
from weather.advice.repository import InMemoryAdviceRepository
from weather.config import AdviceSettings, Settings

NOW = datetime(2026, 8, 4, 6, 0, tzinfo=UTC)
SESSION = "session-abc"


def snapshot(**overrides):
    entry = {
        "location_key": "35.6895,139.6917",
        "name": "Tokyo",
        "country": "Japan",
        "observed_at_utc": (NOW - timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
        "temp_c": 22.0,
        "feelslike_c": 22.0,
        "uv": 3.0,
        "wind_kph": 10.0,
        "precip_chance_next_hour": 5,
        "condition_text": "Sunny",
    }
    entry.update(overrides)
    return {"generated_at_utc": NOW.isoformat(), "locations": [entry]}


def service(advice: AdviceSettings | None = None, provider=None, repo=None) -> AdviceService:
    return AdviceService(
        settings=Settings(advice=advice or AdviceSettings()),
        provider=provider or TemplateAdviceProvider(),
        repository=repo or InMemoryAdviceRepository(),
    )


class TestHappyPath:
    def test_builds_a_card_when_a_rule_matches(self):
        result = service().build(snapshot(precip_chance_next_hour=90), "Tokyo", SESSION, NOW)
        assert result.outcome == AdviceOutcome.GENERATED
        assert result.card.trigger_code == AdviceTrigger.RAIN_EXPECTED

    def test_card_carries_the_full_protocol(self):
        card = service().build(
            snapshot(precip_chance_next_hour=90), "Tokyo", SESSION, NOW
        ).card.to_dict()
        required = {
            "recommendation_id", "location", "trigger_code", "severity", "title",
            "message", "evidence", "weather_observed_at_utc", "generated_at_utc",
            "expires_at_utc", "generation_method", "actions",
        }
        assert required <= set(card)
        assert card["generation_method"] == "template-v1"
        assert [a["type"] for a in card["actions"]] == ["dismiss", "mute"]
        assert card["evidence"][0] == {"label": "降水概率", "value": "90%"}

    def test_observation_time_and_generation_time_are_distinct(self):
        card = service().build(snapshot(precip_chance_next_hour=90), "Tokyo", SESSION, NOW).card
        assert card.weather_observed_at_utc == "2026-08-04T05:50:00Z"
        assert card.generated_at_utc == "2026-08-04T06:00:00Z"
        assert card.expires_at_utc == "2026-08-04T07:00:00Z"

    def test_lookup_by_location_key_also_works(self):
        result = service().build(
            snapshot(precip_chance_next_hour=90), "35.6895,139.6917", SESSION, NOW
        )
        assert result.has_card

    def test_lookup_is_case_insensitive(self):
        assert service().build(
            snapshot(precip_chance_next_hour=90), "tokyo", SESSION, NOW
        ).has_card


class TestNoCard:
    def test_calm_weather_produces_nothing(self):
        result = service().build(snapshot(), "Tokyo", SESSION, NOW)
        assert result.outcome == AdviceOutcome.NO_RULE_MATCHED
        assert result.card is None

    def test_stale_weather_is_refused(self):
        stale = snapshot(
            precip_chance_next_hour=95,
            observed_at_utc=(NOW - timedelta(minutes=200)).isoformat().replace("+00:00", "Z"),
        )
        assert service().build(stale, "Tokyo", SESSION, NOW).outcome == AdviceOutcome.STALE_WEATHER

    def test_freshness_window_is_configurable(self):
        aged = snapshot(
            precip_chance_next_hour=95,
            observed_at_utc=(NOW - timedelta(minutes=200)).isoformat().replace("+00:00", "Z"),
        )
        generous = service(AdviceSettings(max_weather_age_minutes=300))
        assert generous.build(aged, "Tokyo", SESSION, NOW).has_card

    def test_missing_observation_time_is_stale(self):
        assert service().build(
            snapshot(precip_chance_next_hour=95, observed_at_utc=None), "Tokyo", SESSION, NOW
        ).outcome == AdviceOutcome.STALE_WEATHER

    def test_disabled_feature_produces_nothing(self):
        off = service(AdviceSettings(enabled=False))
        assert off.build(snapshot(precip_chance_next_hour=95), "Tokyo", SESSION, NOW).outcome == (
            AdviceOutcome.DISABLED
        )

    def test_unknown_location_raises(self):
        with pytest.raises(InvalidLocation):
            service().build(snapshot(), "Atlantis", SESSION, NOW)


class TestDeduplicationAndFrequency:
    def test_same_snapshot_is_not_shown_twice(self):
        repo = InMemoryAdviceRepository()
        svc = service(repo=repo)
        data = snapshot(precip_chance_next_hour=90)

        assert svc.build(data, "Tokyo", SESSION, NOW).has_card
        second = svc.build(data, "Tokyo", SESSION, NOW + timedelta(minutes=1))
        assert second.outcome == AdviceOutcome.SUPPRESSED_FREQUENCY

    def test_a_new_snapshot_inside_the_window_is_still_suppressed(self):
        repo = InMemoryAdviceRepository()
        svc = service(repo=repo)
        assert svc.build(snapshot(precip_chance_next_hour=90), "Tokyo", SESSION, NOW).has_card

        later = snapshot(
            precip_chance_next_hour=90,
            observed_at_utc=(NOW + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
        )
        result = svc.build(later, "Tokyo", SESSION, NOW + timedelta(minutes=30))
        assert result.outcome == AdviceOutcome.SUPPRESSED_FREQUENCY

    def test_after_the_window_it_may_show_again(self):
        repo = InMemoryAdviceRepository()
        svc = service(repo=repo)
        assert svc.build(snapshot(precip_chance_next_hour=90), "Tokyo", SESSION, NOW).has_card

        much_later = NOW + timedelta(minutes=200)
        fresh = snapshot(
            precip_chance_next_hour=90,
            observed_at_utc=(much_later - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        )
        assert svc.build(fresh, "Tokyo", SESSION, much_later).has_card

    def test_sessions_do_not_share_frequency_state(self):
        repo = InMemoryAdviceRepository()
        svc = service(repo=repo)
        data = snapshot(precip_chance_next_hour=90)
        assert svc.build(data, "Tokyo", "session-1", NOW).has_card
        assert svc.build(data, "Tokyo", "session-2", NOW).has_card


class TestMute:
    def test_mute_suppresses_the_same_trigger(self):
        repo = InMemoryAdviceRepository()
        svc = service(repo=repo)
        data = snapshot(precip_chance_next_hour=90)
        card = svc.build(data, "Tokyo", SESSION, NOW).card

        svc.record_feedback(
            {
                "event": "muted",
                "session_id": SESSION,
                "trigger_code": card.trigger_code,
                "recommendation_id": card.recommendation_id,
            },
            now=NOW,
        )

        much_later = NOW + timedelta(minutes=300)
        fresh = snapshot(
            precip_chance_next_hour=90,
            observed_at_utc=(much_later - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        )
        assert svc.build(fresh, "Tokyo", SESSION, much_later).outcome == (
            AdviceOutcome.SUPPRESSED_MUTED
        )

    def test_mute_is_per_trigger(self):
        repo = InMemoryAdviceRepository()
        svc = service(repo=repo)
        svc.record_feedback(
            {"event": "muted", "session_id": SESSION, "trigger_code": "RAIN_EXPECTED"}, now=NOW
        )
        assert svc.build(snapshot(temp_c=40.0), "Tokyo", SESSION, NOW).has_card

    def test_mute_expires_with_the_day(self):
        repo = InMemoryAdviceRepository()
        svc = service(repo=repo)
        svc.record_feedback(
            {"event": "muted", "session_id": SESSION, "trigger_code": "RAIN_EXPECTED"}, now=NOW
        )
        tomorrow = datetime(2026, 8, 5, 1, 0, tzinfo=UTC)
        fresh = snapshot(
            precip_chance_next_hour=90,
            observed_at_utc=(tomorrow - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        )
        assert svc.build(fresh, "Tokyo", SESSION, tomorrow).has_card


class TestEscalation:
    def test_a_more_severe_risk_breaks_through_frequency_control(self):
        repo = InMemoryAdviceRepository()
        svc = service(repo=repo)
        # An info-level rain card first.
        assert svc.build(snapshot(precip_chance_next_hour=90), "Tokyo", SESSION, NOW).has_card

        # Minutes later the heat rule fires, which is a warning. Being quiet
        # here would mean withholding the more serious message.
        soon = NOW + timedelta(minutes=5)
        hotter = snapshot(
            temp_c=41.0,
            observed_at_utc=(soon - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        )
        result = svc.build(hotter, "Tokyo", SESSION, soon)
        assert result.has_card
        assert result.card.trigger_code == AdviceTrigger.EXTREME_HEAT

    def test_escalation_also_overrides_a_mute(self):
        repo = InMemoryAdviceRepository()
        svc = service(repo=repo)
        svc.build(snapshot(precip_chance_next_hour=90), "Tokyo", SESSION, NOW)
        svc.record_feedback(
            {"event": "muted", "session_id": SESSION, "trigger_code": "RAIN_EXPECTED"}, now=NOW
        )
        soon = NOW + timedelta(minutes=5)
        hotter = snapshot(
            temp_c=41.0,
            observed_at_utc=(soon - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        )
        assert svc.build(hotter, "Tokyo", SESSION, soon).has_card


class TestProviderFailure:
    def test_a_broken_provider_degrades_to_no_card(self):
        class Exploding:
            name = "exploding-v1"

            def generate(self, trigger, weather):
                raise RuntimeError("model unavailable")

        result = service(provider=Exploding()).build(
            snapshot(precip_chance_next_hour=90), "Tokyo", SESSION, NOW
        )
        assert result.outcome == AdviceOutcome.PROVIDER_FAILURE
        assert result.card is None

    def test_a_replacement_provider_needs_no_other_change(self):
        # The seam phase two uses: swap the provider, keep the rules, the
        # policy and the card protocol untouched.
        class Fake:
            name = "rag-v0"

            def generate(self, trigger, weather):
                return AdviceContent(
                    title="检索生成的标题",
                    message="检索生成的正文。",
                    generation_method=self.name,
                )

        card = service(provider=Fake()).build(
            snapshot(precip_chance_next_hour=90), "Tokyo", SESSION, NOW
        ).card
        assert card.generation_method == "rag-v0"
        assert card.title == "检索生成的标题"
        # Evidence still comes from the rule, not from the provider.
        assert card.evidence[0].value == "90%"


class TestFeedback:
    @pytest.mark.parametrize(
        "event", ["shown", "clicked", "dismissed", "muted", "helpful", "not_helpful"]
    )
    def test_accepts_every_documented_event(self, event):
        record = service().record_feedback(
            {"event": event, "session_id": SESSION, "trigger_code": "HIGH_UV"}, now=NOW
        )
        assert record["event"] == event
        assert record["event_at_utc"] == "2026-08-04T06:00:00Z"

    def test_rejects_an_unknown_event(self):
        with pytest.raises(ValueError, match="unknown event"):
            service().record_feedback({"event": "exploded", "session_id": SESSION})

    def test_requires_a_session(self):
        with pytest.raises(ValueError, match="session_id"):
            service().record_feedback({"event": "shown", "session_id": ""})

    def test_record_carries_the_required_fields_and_nothing_personal(self):
        record = service().record_feedback(
            {
                "event": "helpful",
                "session_id": SESSION,
                "trigger_code": "RAIN_EXPECTED",
                "location": "Tokyo",
                "weather_snapshot_id": "abc123",
                "generation_method": "template-v1",
                "recommendation_id": "rec-1",
            },
            now=NOW,
        )
        assert set(record) == {
            "recommendation_id", "trigger_code", "location", "weather_snapshot_id",
            "generation_method", "event", "event_at_utc", "session_id", "rule_version",
        }

    def test_logs_a_structured_line(self, caplog):
        with caplog.at_level("INFO", logger="weather.advice.service"):
            service().record_feedback(
                {"event": "dismissed", "session_id": SESSION, "trigger_code": "HIGH_UV"}, now=NOW
            )
        assert any("ADVICE_FEEDBACK" in r.message for r in caplog.records)


class TestObservability:
    @pytest.mark.parametrize(
        ("data", "marker"),
        [
            ({"precip_chance_next_hour": 90}, "ADVICE_GENERATED"),
            ({}, "ADVICE_NO_RULE_MATCHED"),
        ],
    )
    def test_each_outcome_is_logged(self, caplog, data, marker):
        with caplog.at_level("INFO", logger="weather.advice.service"):
            service().build(snapshot(**data), "Tokyo", SESSION, NOW)
        assert any(marker in r.message for r in caplog.records)
