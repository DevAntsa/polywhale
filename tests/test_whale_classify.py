from pathlib import Path

from polywhale.db import connect, run_migrations
from polywhale.polymarket import LeaderboardRow
from polywhale.whale_classify import (
    SHAPE_ARB_OP,
    SHAPE_HYBRID,
    SHAPE_SHARP,
    SHAPE_UNKNOWN,
    classify_shape,
    fetch_and_classify,
    persist_profiles,
    top_arb_ops,
    top_sharps,
)


def test_classify_shape_thresholds() -> None:
    assert classify_shape(50, 100) == SHAPE_UNKNOWN  # under 1M volume
    assert classify_shape(60_000, 1_000_000) == SHAPE_SHARP  # 6% margin
    assert classify_shape(15_000, 1_000_000) == SHAPE_ARB_OP  # 1.5%
    assert classify_shape(25_000, 1_000_000) == SHAPE_HYBRID  # 2.5%
    assert classify_shape(30_000, 1_000_000) == SHAPE_SHARP  # exactly 3%
    assert (
        classify_shape(20_000, 1_000_000) == SHAPE_HYBRID
    )  # exactly 2% -> hybrid (>= arb threshold)


class _StubClient:
    def __init__(self, profits, volumes):
        self._profits = profits
        self._volumes = volumes

    def get_leaderboard(self, metric, *, window="30d"):
        if metric == "profit":
            return list(self._profits)
        if metric == "volume":
            return list(self._volumes)
        raise ValueError(metric)


def test_fetch_and_classify_joins_profit_and_volume() -> None:
    profits = [
        LeaderboardRow("0xsharp", "sharp_user", "Sharp", 3_500_000),
        LeaderboardRow("0xarb", "arb_user", "Arb", 1_500_000),
        LeaderboardRow("0xunknown", "small", "Small", 5_000),
    ]
    volumes = [
        LeaderboardRow("0xsharp", "sharp_user", "Sharp", 50_000_000),  # 7% margin
        LeaderboardRow("0xarb", "arb_user", "Arb", 150_000_000),  # 1% margin
        LeaderboardRow("0xunknown", "small", "Small", 100_000),  # too small
    ]
    client = _StubClient(profits, volumes)
    profiles = fetch_and_classify(client, window="30d", top_n=10)
    by_wallet = {p.wallet: p for p in profiles}
    assert by_wallet["0xsharp"].shape == SHAPE_SHARP
    assert by_wallet["0xarb"].shape == SHAPE_ARB_OP
    assert by_wallet["0xunknown"].shape == SHAPE_UNKNOWN
    assert abs(by_wallet["0xsharp"].margin_pct - 7.0) < 0.01


def test_top_sharps_and_arb_ops_sort_correctly() -> None:
    profits = [
        LeaderboardRow("0xs1", "S1", "S1", 5_000_000),  # 5% margin
        LeaderboardRow("0xs2", "S2", "S2", 4_000_000),  # 4%
        LeaderboardRow("0xa1", "A1", "A1", 1_200_000),  # 1.2%
        LeaderboardRow("0xa2", "A2", "A2", 800_000),  # 0.8%
    ]
    volumes = [
        LeaderboardRow("0xs1", "S1", "S1", 100_000_000),
        LeaderboardRow("0xs2", "S2", "S2", 100_000_000),
        LeaderboardRow("0xa1", "A1", "A1", 100_000_000),
        LeaderboardRow("0xa2", "A2", "A2", 200_000_000),
    ]
    client = _StubClient(profits, volumes)
    profiles = fetch_and_classify(client, top_n=10)
    sharps = top_sharps(profiles, n=2)
    assert [p.wallet for p in sharps] == ["0xs1", "0xs2"]
    arbs = top_arb_ops(profiles, n=2)
    assert [p.wallet for p in arbs] == ["0xa2", "0xa1"]


def test_persist_profiles_writes_rows(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite"
    conn = connect(db)
    try:
        run_migrations(conn)
        profits = [LeaderboardRow("0xsharp", "S", "S", 3_500_000)]
        volumes = [LeaderboardRow("0xsharp", "S", "S", 50_000_000)]
        client = _StubClient(profits, volumes)
        profiles = fetch_and_classify(client, top_n=10)
        stored = persist_profiles(conn, profiles)
        assert stored == 1
        row = conn.execute("SELECT wallet, shape, margin_pct FROM whale_profiles").fetchone()
        assert row["wallet"] == "0xsharp"
        assert row["shape"] == SHAPE_SHARP
        assert abs(row["margin_pct"] - 7.0) < 0.01
    finally:
        conn.close()
