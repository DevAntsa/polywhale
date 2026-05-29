"""Tests for the leaderboard discovery sweep."""

from polywhale.polymarket import LeaderboardRow, WhalePosition
from polywhale.whale_discovery import Candidate, discover_candidates
from polywhale.whale_stats import ActivityStats


class _StubClient:
    """Composable stub for the four client methods discovery touches."""

    def __init__(
        self,
        *,
        profit_rows: list[LeaderboardRow],
        volume_rows: list[LeaderboardRow],
        positions_by_wallet: dict[str, list[WhalePosition]],
        activity_by_wallet: dict[str, ActivityStats],
    ) -> None:
        self.profit_rows = profit_rows
        self.volume_rows = volume_rows
        self.positions_by_wallet = positions_by_wallet
        self.activity_by_wallet = activity_by_wallet

    def get_leaderboard(self, metric: str, *, window=None):
        return self.profit_rows if metric == "profit" else self.volume_rows

    def get_whale_positions(self, wallet: str, *, size_threshold=0.0):
        return self.positions_by_wallet.get(wallet.lower(), [])

    def get_activity(self, wallet: str, *, limit=500):
        # discover_candidates calls compute_activity_stats which calls get_activity
        # but our stub bypasses that by mocking compute_activity_stats. Kept here
        # so any code path that does call it doesn't blow up.
        return []


def _lb(wallet: str, amount: float, pseudo: str | None = None) -> LeaderboardRow:
    return LeaderboardRow(
        wallet=wallet, pseudonym=pseudo, name=pseudo, amount=amount,
    )


def _pos(wallet: str, current_value: float) -> WhalePosition:
    return WhalePosition(
        wallet=wallet, asset_id="t", condition_id=None,
        market_slug="m", event_slug="e", title="t", outcome="Yes",
        size=100.0, avg_price=0.5, current_price=0.5,
        current_value=current_value, initial_value=50.0,
        cash_pnl=0.0, realized_pnl=0.0, percent_pnl=0.0,
        end_date=None, neg_risk=False,
    )


def _stats(n: int, wr: float | None) -> ActivityStats:
    return ActivityStats(
        wallet="0x", last_trade_at=1000, unique_markets=n,
        won_markets=int(n * (wr or 0) / 100), win_rate_pct=wr,
        sample_size=n, pulled_events=n * 2,
    )


def _run_with_stub_activity(stub, *, activity_overrides, **kwargs):
    """Monkeypatch compute_activity_stats so discovery uses the stubbed result."""
    import polywhale.whale_discovery as wd
    orig = wd.compute_activity_stats
    try:
        wd.compute_activity_stats = lambda client, wallet: activity_overrides.get(
            wallet, _stats(0, None),
        )
        return discover_candidates(stub, **kwargs)
    finally:
        wd.compute_activity_stats = orig


def test_discover_filters_by_volume() -> None:
    """A wallet under the volume floor should not surface."""
    stub = _StubClient(
        profit_rows=[_lb("0xa", 100_000.0, "Alpha"), _lb("0xb", 50_000.0, "Beta")],
        volume_rows=[
            _lb("0xa", 500_000.0, "Alpha"),
            _lb("0xb", 100_000.0, "Beta"),    # under $300K floor
        ],
        positions_by_wallet={
            "0xa": [_pos("0xa", 50_000.0)],
            "0xb": [_pos("0xb", 10_000.0)],
        },
        activity_by_wallet={},
    )
    out = _run_with_stub_activity(
        stub,
        activity_overrides={
            "0xa": _stats(20, 75.0),
            "0xb": _stats(20, 80.0),
        },
        leaderboard_depth=10,
        min_volume_usd=300_000.0,
    )
    wallets = {c.wallet for c in out}
    assert "0xa" in wallets
    assert "0xb" not in wallets


def test_discover_filters_by_portfolio_value() -> None:
    """A wallet with zero portfolio value should not surface."""
    stub = _StubClient(
        profit_rows=[_lb("0xa", 100_000.0), _lb("0xb", 100_000.0)],
        volume_rows=[
            _lb("0xa", 500_000.0), _lb("0xb", 500_000.0),
        ],
        positions_by_wallet={
            "0xa": [_pos("0xa", 50_000.0)],
            "0xb": [],  # no open positions
        },
        activity_by_wallet={},
    )
    out = _run_with_stub_activity(
        stub,
        activity_overrides={
            "0xa": _stats(20, 75.0),
            "0xb": _stats(20, 80.0),
        },
        leaderboard_depth=10,
    )
    wallets = {c.wallet for c in out}
    assert "0xa" in wallets
    assert "0xb" not in wallets


def test_discover_filters_by_win_rate() -> None:
    """A wallet with WR below 70% should not surface even if other gates pass."""
    stub = _StubClient(
        profit_rows=[_lb("0xa", 100_000.0)],
        volume_rows=[_lb("0xa", 500_000.0)],
        positions_by_wallet={"0xa": [_pos("0xa", 50_000.0)]},
        activity_by_wallet={},
    )
    out = _run_with_stub_activity(
        stub,
        activity_overrides={"0xa": _stats(50, 60.0)},
        leaderboard_depth=10,
    )
    assert out == []


def test_discover_filters_by_min_trades() -> None:
    """A wallet with too few resolved trades should not surface."""
    stub = _StubClient(
        profit_rows=[_lb("0xa", 100_000.0)],
        volume_rows=[_lb("0xa", 500_000.0)],
        positions_by_wallet={"0xa": [_pos("0xa", 50_000.0)]},
        activity_by_wallet={},
    )
    out = _run_with_stub_activity(
        stub,
        activity_overrides={"0xa": _stats(5, 95.0)},  # only 5 trades
        leaderboard_depth=10,
    )
    assert out == []


def test_discover_ranks_by_skill_weighted_capital() -> None:
    """Higher WR + bigger volume should rank above lower WR even with same volume."""
    stub = _StubClient(
        profit_rows=[
            _lb("0xa", 100_000.0, "A"),
            _lb("0xb", 100_000.0, "B"),
        ],
        volume_rows=[
            _lb("0xa", 1_000_000.0, "A"),
            _lb("0xb", 500_000.0, "B"),
        ],
        positions_by_wallet={
            "0xa": [_pos("0xa", 50_000.0)],
            "0xb": [_pos("0xb", 50_000.0)],
        },
        activity_by_wallet={},
    )
    out = _run_with_stub_activity(
        stub,
        activity_overrides={
            "0xa": _stats(20, 75.0),
            "0xb": _stats(20, 90.0),
        },
        leaderboard_depth=10,
    )
    assert len(out) == 2
    # A has 75% * sqrt(1000) = 75 * 31.6 ≈ 2372
    # B has 90% * sqrt(500) = 90 * 22.4 ≈ 2012
    # A should rank above B
    assert out[0].wallet == "0xa"


def test_candidate_fields_are_populated() -> None:
    stub = _StubClient(
        profit_rows=[_lb("0xa", 250_000.0, "Sharp")],
        volume_rows=[_lb("0xa", 800_000.0, "Sharp")],
        positions_by_wallet={"0xa": [_pos("0xa", 120_000.0)]},
        activity_by_wallet={},
    )
    out = _run_with_stub_activity(
        stub,
        activity_overrides={"0xa": _stats(34, 82.4)},
        leaderboard_depth=10,
    )
    assert len(out) == 1
    c = out[0]
    assert isinstance(c, Candidate)
    assert c.wallet == "0xa"
    assert c.pseudonym == "Sharp"
    assert c.profit == 250_000.0
    assert c.volume == 800_000.0
    assert c.portfolio_value == 120_000.0
    assert c.n_resolved == 34
    assert c.win_rate_pct == 82.4
    assert c.rank_score > 0
