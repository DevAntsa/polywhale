import time

from polywhale.whale_stats import (
    ActivityStats,
    compute_activity_stats,
    passes_activity_filter,
)


class _StubClient:
    def __init__(self, events: list[dict] | None = None, raise_on_call: bool = False) -> None:
        self._events = events or []
        self._raise = raise_on_call
        self.calls = 0

    def get_activity(self, wallet: str, *, limit: int = 500) -> list[dict]:
        self.calls += 1
        if self._raise:
            raise RuntimeError("api down")
        return self._events


def test_compute_activity_stats_empty_history() -> None:
    client = _StubClient(events=[])
    stats = compute_activity_stats(client, "0xw")
    assert stats.last_trade_at is None
    assert stats.unique_markets == 0
    assert stats.won_markets == 0
    assert stats.win_rate_pct is None


def test_compute_activity_stats_basic_wr() -> None:
    events = [
        {"type": "TRADE", "conditionId": "m1", "timestamp": 1000},
        {"type": "TRADE", "conditionId": "m1", "timestamp": 1100},  # same market
        {"type": "TRADE", "conditionId": "m2", "timestamp": 1200},
        {"type": "TRADE", "conditionId": "m3", "timestamp": 1300},
        {"type": "TRADE", "conditionId": "m4", "timestamp": 1400},
        {"type": "TRADE", "conditionId": "m5", "timestamp": 1500},
        {"type": "REDEEM", "conditionId": "m1", "timestamp": 2000},
        {"type": "REDEEM", "conditionId": "m2", "timestamp": 2100},
        {"type": "REDEEM", "conditionId": "m3", "timestamp": 2200},
    ]
    stats = compute_activity_stats(_StubClient(events), "0xw", min_samples=5)
    assert stats.unique_markets == 5         # m1..m5
    assert stats.won_markets == 3            # m1, m2, m3
    assert stats.win_rate_pct == 60.0
    assert stats.last_trade_at == 1500       # newest TRADE timestamp (REDEEMs don't count)


def test_compute_activity_stats_handles_api_error() -> None:
    stats = compute_activity_stats(_StubClient(raise_on_call=True), "0xw")
    assert stats.last_trade_at is None
    assert stats.unique_markets == 0
    assert stats.win_rate_pct is None


def test_compute_activity_stats_below_sample_returns_no_wr() -> None:
    events = [
        {"type": "TRADE", "conditionId": "m1", "timestamp": 100},
        {"type": "TRADE", "conditionId": "m2", "timestamp": 200},
        {"type": "REDEEM", "conditionId": "m1", "timestamp": 300},
    ]
    stats = compute_activity_stats(_StubClient(events), "0xw", min_samples=5)
    assert stats.unique_markets == 2
    assert stats.win_rate_pct is None  # below min_samples


def test_passes_activity_filter_recent_and_high_wr() -> None:
    now = int(time.time())
    stats = ActivityStats(
        wallet="0xw", last_trade_at=now - 86400, unique_markets=30,
        won_markets=21, win_rate_pct=70.0, sample_size=30, pulled_events=100,
    )
    ok, reason = passes_activity_filter(stats, now_ts=now)
    assert ok is True
    assert reason == "ok"


def test_passes_activity_filter_rejects_dormant() -> None:
    now = int(time.time())
    stats = ActivityStats(
        wallet="0xw", last_trade_at=now - 30 * 86400, unique_markets=30,
        won_markets=21, win_rate_pct=70.0, sample_size=30, pulled_events=100,
    )
    ok, reason = passes_activity_filter(stats, max_dormant_days=14, now_ts=now)
    assert ok is False
    assert "dormant" in reason


def test_passes_activity_filter_rejects_low_wr() -> None:
    now = int(time.time())
    stats = ActivityStats(
        wallet="0xw", last_trade_at=now - 3600, unique_markets=30,
        won_markets=12, win_rate_pct=40.0, sample_size=30, pulled_events=100,
    )
    ok, reason = passes_activity_filter(stats, min_wr_pct=60.0, now_ts=now)
    assert ok is False
    assert "wr_below" in reason


def test_passes_activity_filter_rejects_small_sample() -> None:
    now = int(time.time())
    stats = ActivityStats(
        wallet="0xw", last_trade_at=now - 3600, unique_markets=5,
        won_markets=5, win_rate_pct=100.0, sample_size=5, pulled_events=10,
    )
    ok, reason = passes_activity_filter(stats, min_sample=20, now_ts=now)
    assert ok is False
    assert "sample_too_small" in reason


def test_passes_activity_filter_rejects_no_trade_history() -> None:
    stats = ActivityStats(
        wallet="0xw", last_trade_at=None, unique_markets=0,
        won_markets=0, win_rate_pct=None, sample_size=0, pulled_events=0,
    )
    ok, reason = passes_activity_filter(stats)
    assert ok is False
    assert reason == "no_trade_history"
