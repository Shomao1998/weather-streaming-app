"""Rule engine: thresholds, boundaries and priority."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from weather.advice import AdviceTrigger, Severity, WeatherContext
from weather.advice import rules as advice_rules
from weather.config import AdviceSettings

SETTINGS = AdviceSettings()
OBSERVED = datetime(2026, 8, 4, 6, 0, tzinfo=UTC)


def ctx(**overrides) -> WeatherContext:
    base = {
        "location": "Tokyo",
        "location_key": "35.6895,139.6917",
        "observed_at_utc": OBSERVED,
        "temp_c": 22.0,
        "feelslike_c": 22.0,
        "uv": 3.0,
        "wind_kph": 10.0,
        "precip_chance_next_hour": 5,
    }
    base.update(overrides)
    return WeatherContext(**base)


class TestQuietWeather:
    def test_pleasant_conditions_match_nothing(self):
        assert advice_rules.evaluate(ctx(), SETTINGS) == []
        assert advice_rules.top_match(ctx(), SETTINGS) is None


class TestRainRule:
    def test_fires_exactly_at_the_threshold(self):
        match = advice_rules.top_match(ctx(precip_chance_next_hour=80), SETTINGS)
        assert match is not None
        assert match.trigger == AdviceTrigger.RAIN_EXPECTED

    def test_one_below_the_threshold_stays_quiet(self):
        assert advice_rules.top_match(ctx(precip_chance_next_hour=79), SETTINGS) is None

    def test_missing_forecast_is_not_treated_as_dry(self):
        # No hourly forecast covered the window. "Unknown" must not become
        # "no rain" — the rule declines instead of guessing.
        assert advice_rules.top_match(ctx(precip_chance_next_hour=None), SETTINGS) is None

    def test_evidence_quotes_the_probability(self):
        match = advice_rules.top_match(ctx(precip_chance_next_hour=85), SETTINGS)
        assert match.evidence[0].label == "降水概率"
        assert match.evidence[0].value == "85%"

    def test_threshold_is_configurable(self):
        lenient = AdviceSettings(rain_chance_percent=50)
        assert advice_rules.top_match(ctx(precip_chance_next_hour=60), lenient) is not None
        assert advice_rules.top_match(ctx(precip_chance_next_hour=60), SETTINGS) is None


class TestUvRule:
    def test_fires_at_the_threshold(self):
        match = advice_rules.top_match(ctx(uv=8.0), SETTINGS)
        assert match.trigger == AdviceTrigger.HIGH_UV
        assert match.severity == Severity.WARNING

    def test_just_below_stays_quiet(self):
        assert advice_rules.top_match(ctx(uv=7.9), SETTINGS) is None

    def test_missing_uv_is_ignored(self):
        assert advice_rules.top_match(ctx(uv=None), SETTINGS) is None


class TestHeatRule:
    def test_fires_at_the_threshold(self):
        match = advice_rules.top_match(ctx(temp_c=35.0), SETTINGS)
        assert match.trigger == AdviceTrigger.EXTREME_HEAT

    def test_just_below_stays_quiet(self):
        assert advice_rules.top_match(ctx(temp_c=34.9), SETTINGS) is None

    def test_includes_feels_like_when_present(self):
        match = advice_rules.top_match(ctx(temp_c=36.0, feelslike_c=41.0), SETTINGS)
        labels = [e.label for e in match.evidence]
        assert labels == ["气温", "体感温度"]

    def test_omits_feels_like_when_absent(self):
        match = advice_rules.top_match(ctx(temp_c=36.0, feelslike_c=None), SETTINGS)
        assert [e.label for e in match.evidence] == ["气温"]


class TestWindRule:
    def test_fires_at_the_threshold(self):
        match = advice_rules.top_match(ctx(wind_kph=40.0), SETTINGS)
        assert match.trigger == AdviceTrigger.HIGH_WIND

    def test_just_below_stays_quiet(self):
        assert advice_rules.top_match(ctx(wind_kph=39.9), SETTINGS) is None


class TestPriority:
    def test_all_four_can_match_at_once(self):
        severe = ctx(temp_c=40.0, uv=11.0, wind_kph=70.0, precip_chance_next_hour=95)
        assert len(advice_rules.evaluate(severe, SETTINGS)) == 4

    def test_heat_outranks_everything_else(self):
        severe = ctx(temp_c=40.0, uv=11.0, wind_kph=70.0, precip_chance_next_hour=95)
        assert advice_rules.top_match(severe, SETTINGS).trigger == AdviceTrigger.EXTREME_HEAT

    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            ({"wind_kph": 70.0, "uv": 11.0}, AdviceTrigger.HIGH_WIND),
            ({"uv": 11.0, "precip_chance_next_hour": 95}, AdviceTrigger.HIGH_UV),
            ({"precip_chance_next_hour": 95}, AdviceTrigger.RAIN_EXPECTED),
        ],
    )
    def test_pairwise_ordering(self, overrides, expected):
        assert advice_rules.top_match(ctx(**overrides), SETTINGS).trigger == expected

    def test_ordering_is_by_priority_not_declaration(self):
        matches = advice_rules.evaluate(
            ctx(temp_c=40.0, uv=11.0, wind_kph=70.0, precip_chance_next_hour=95), SETTINGS
        )
        assert [m.priority for m in matches] == sorted(m.priority for m in matches)


class TestSnapshotIdentity:
    def test_same_observation_gives_the_same_snapshot_id(self):
        assert ctx().snapshot_id == ctx(temp_c=99.0).snapshot_id

    def test_a_new_observation_changes_it(self):
        later = ctx(observed_at_utc=datetime(2026, 8, 4, 7, 0, tzinfo=UTC))
        assert ctx().snapshot_id != later.snapshot_id

    def test_locations_do_not_collide(self):
        assert ctx().snapshot_id != ctx(location_key="34.69,135.50").snapshot_id
