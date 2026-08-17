"""The cost guard: the in-code ceiling that keeps the paid path affordable.

Everything here is about one property — a paid call is granted only when both
the per-session throttle and the global daily budget allow it, and *any*
uncertainty denies. The tests inject a fake spend store and a fake clock so the
limits are exercised without Azure or real time.
"""

from __future__ import annotations

from weather.assistant_guard import AssistantGuard
from weather.config import RagSettings, Settings


class FakeStore:
    """An in-memory stand-in for the blob spend store."""

    def __init__(self, spent: float = 0.0, raise_on_read: bool = False) -> None:
        self.spent = spent
        self.raise_on_read = raise_on_read
        self.recorded: list[float] = []

    def read_spend(self) -> float:
        if self.raise_on_read:
            raise RuntimeError("storage unavailable")
        return self.spent

    def add_spend(self, cost_usd: float) -> None:
        self.spent += cost_usd
        self.recorded.append(cost_usd)


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def settings(**rag) -> Settings:
    defaults = {"daily_budget_usd": 1.0, "session_max_calls": 3, "session_window_seconds": 60}
    defaults.update(rag)
    return Settings(rag=RagSettings(**defaults))


def guard(store, clock=None, **rag):
    s = settings(**rag)
    return AssistantGuard(s, store=store, clock=clock or Clock()), s


# -- the happy path ---------------------------------------------------------


def test_allows_a_call_when_under_both_limits():
    g, s = guard(FakeStore(spent=0.0))
    assert g.check("sess-a", s).allowed is True


# -- the global daily budget ------------------------------------------------


def test_denies_once_the_daily_budget_is_reached():
    g, s = guard(FakeStore(spent=1.0), daily_budget_usd=1.0)
    d = g.check("sess-a", s)
    assert d.allowed is False and "budget" in d.reason


def test_spend_accrues_across_calls_until_the_cap():
    store = FakeStore(spent=0.0)
    g, s = guard(store, daily_budget_usd=0.01)
    # Under budget → allowed; record two calls that together cross the cap.
    assert g.check("s", s).allowed
    g.record("s", 0.006, s)
    g.record("s", 0.006, s)  # total 0.012 > 0.01
    assert g.check("s", s).allowed is False
    assert store.recorded == [0.006, 0.006]


def test_a_zero_or_negative_budget_disables_the_paid_path_entirely():
    g, s = guard(FakeStore(spent=0.0), daily_budget_usd=0.0)
    assert g.check("s", s).allowed is False


# -- fail closed ------------------------------------------------------------


def test_a_storage_error_denies_rather_than_risking_overspend():
    g, s = guard(FakeStore(raise_on_read=True))
    d = g.check("s", s)
    assert d.allowed is False and "unavailable" in d.reason


# -- the per-session throttle ----------------------------------------------


def test_a_session_is_throttled_after_its_window_fills():
    clock = Clock(1000.0)
    g, s = guard(FakeStore(spent=0.0), clock=clock, session_max_calls=3, session_window_seconds=60)
    for _ in range(3):
        assert g.check("busy", s).allowed
        g.record("busy", 0.0001, s)
    # The fourth call within the window is refused for this session…
    assert g.check("busy", s).allowed is False
    # …but a different session is unaffected.
    assert g.check("other", s).allowed is True


def test_the_session_window_slides():
    clock = Clock(1000.0)
    g, s = guard(FakeStore(spent=0.0), clock=clock, session_max_calls=2, session_window_seconds=60)
    g.record("s", 0.0, s)
    g.record("s", 0.0, s)
    assert g.check("s", s).allowed is False
    clock.t += 61  # the two hits age out of the window
    assert g.check("s", s).allowed is True


def test_the_session_throttle_is_checked_before_the_budget_read():
    # A throttled session must not even hit storage — cheap rejection first.
    store = FakeStore(raise_on_read=True)  # would raise if read
    clock = Clock(1000.0)
    g, s = guard(store, clock=clock, session_max_calls=1, session_window_seconds=60)
    # First call: session ok, but budget read raises → denied "unavailable".
    assert g.check("s", s).reason == "budget unavailable"
    g.record("s", 0.0, s)  # fill the session window
    # Second call: session throttle fires first, so the reason is the throttle,
    # proving the storage read was skipped.
    assert g.check("s", s).reason == "session rate limit"
