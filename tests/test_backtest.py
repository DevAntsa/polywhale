import time
from pathlib import Path

from polywhale.backtest import (
    collect_signals,
    resolve_bets,
    summarize,
    synthesize_bets,
)
from polywhale.db import connect, run_migrations
from polywhale.polymarket import PolyMarket


def _insert_signal(
    conn,
    *,
    wallet: str,
    asset_id: str,
    market_slug: str,
    current_price: float,
    signal_type: str = "new_position",
    detected_at: int = 0,
    conviction_discount: float | None = 1.0,
    title: str = "Match",
    outcome: str = "Yes",
) -> None:
    if detected_at == 0:
        detected_at = int(time.time())
    conn.execute(
        """
        INSERT INTO whale_signals(
            wallet, signal_type, asset_id, market_slug, title, outcome,
            old_size, new_size, current_price, prev_captured_at,
            latest_captured_at, detected_at, conviction_discount
        ) VALUES (?, ?, ?, ?, ?, ?, NULL, 100, ?, 1, 2, ?, ?)
        """,
        (
            wallet,
            signal_type,
            asset_id,
            market_slug,
            title,
            outcome,
            current_price,
            detected_at,
            conviction_discount,
        ),
    )
    conn.commit()


class _StubClient:
    def __init__(self, markets: dict[str, PolyMarket | None]) -> None:
        self._markets = markets

    def get_market(self, slug: str) -> PolyMarket | None:
        return self._markets.get(slug)


def _resolved_market(slug: str, token_ids: list[str], winner_idx: int) -> PolyMarket:
    prices = [0.0] * len(token_ids)
    prices[winner_idx] = 1.0
    return PolyMarket(
        slug=slug,
        question="?",
        outcomes=["Y", "N"][: len(token_ids)],
        outcome_prices=prices,
        volume_24h=0,
        volume_total=0,
        category=None,
        closed=True,
        end_date=None,
        token_ids=token_ids,
    )


def _open_market(slug: str, token_ids: list[str]) -> PolyMarket:
    return PolyMarket(
        slug=slug,
        question="?",
        outcomes=["Y", "N"][: len(token_ids)],
        outcome_prices=[0.5, 0.5],
        volume_24h=0,
        volume_total=0,
        category=None,
        closed=False,
        end_date=None,
        token_ids=token_ids,
    )


def test_collect_signals_respects_since_days(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        now = int(time.time())
        _insert_signal(conn, wallet="0xa", asset_id="t1", market_slug="m1",
                       current_price=0.40, detected_at=now - 86400 * 5)
        _insert_signal(conn, wallet="0xb", asset_id="t2", market_slug="m2",
                       current_price=0.40, detected_at=now - 86400 * 60)
        rows = collect_signals(conn, since_days=30)
        assert len(rows) == 1
        assert rows[0]["wallet"] == "0xa"
    finally:
        conn.close()


def test_collect_signals_filters_by_min_conviction(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _insert_signal(conn, wallet="0xa", asset_id="t1", market_slug="m1",
                       current_price=0.40, conviction_discount=1.0)
        _insert_signal(conn, wallet="0xb", asset_id="t2", market_slug="m2",
                       current_price=0.40, conviction_discount=0.5)
        rows = collect_signals(conn, since_days=30, min_conviction=0.7)
        assert len(rows) == 1
        assert rows[0]["wallet"] == "0xa"
    finally:
        conn.close()


def test_synthesize_bets_weights_by_conviction(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _insert_signal(conn, wallet="0xa", asset_id="t1", market_slug="m1",
                       current_price=0.40, conviction_discount=0.5)
        rows = collect_signals(conn, since_days=30)
        bets = synthesize_bets(rows, stake_per_signal=100.0, weight_by_conviction=True)
        assert len(bets) == 1
        assert bets[0].stake_usd == 50.0  # 100 * 0.5
        # With weighting disabled:
        bets2 = synthesize_bets(rows, stake_per_signal=100.0, weight_by_conviction=False)
        assert bets2[0].stake_usd == 100.0
    finally:
        conn.close()


def test_synthesize_skips_bad_prices(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _insert_signal(conn, wallet="0xa", asset_id="t1", market_slug="m1",
                       current_price=0.0)
        _insert_signal(conn, wallet="0xb", asset_id="t2", market_slug="m2",
                       current_price=1.0)
        _insert_signal(conn, wallet="0xc", asset_id="t3", market_slug="m3",
                       current_price=0.40)
        rows = collect_signals(conn, since_days=30)
        bets = synthesize_bets(rows)
        assert len(bets) == 1
        assert bets[0].wallet == "0xc"
    finally:
        conn.close()


def test_resolve_bets_marks_winner(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _insert_signal(conn, wallet="0xa", asset_id="tok-YES", market_slug="m1",
                       current_price=0.40)
        rows = collect_signals(conn, since_days=30)
        bets = synthesize_bets(rows, weight_by_conviction=False)
        client = _StubClient({"m1": _resolved_market("m1", ["tok-YES", "tok-NO"], 0)})
        resolved = resolve_bets(bets, client)
        assert len(resolved) == 1
        b = resolved[0]
        assert b.resolved
        assert b.won is True
        # stake 100, entry 0.40 -> shares 250, payout 1 -> pnl = (1-0.40)*250 = 150
        assert abs(b.pnl - 150.0) < 1e-3
    finally:
        conn.close()


def test_resolve_bets_marks_loser(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _insert_signal(conn, wallet="0xa", asset_id="tok-YES", market_slug="m1",
                       current_price=0.40)
        rows = collect_signals(conn, since_days=30)
        bets = synthesize_bets(rows, weight_by_conviction=False)
        # Yes lost: token 'tok-NO' wins.
        client = _StubClient({"m1": _resolved_market("m1", ["tok-YES", "tok-NO"], 1)})
        resolved = resolve_bets(bets, client)
        b = resolved[0]
        assert b.resolved
        assert b.won is False
        # stake 100, entry 0.40 -> shares 250, payout 0 -> pnl = -0.40 * 250 = -100
        assert abs(b.pnl + 100.0) < 1e-3
    finally:
        conn.close()


def test_resolve_bets_leaves_unresolved_untouched(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _insert_signal(conn, wallet="0xa", asset_id="tok1", market_slug="m1",
                       current_price=0.40)
        rows = collect_signals(conn, since_days=30)
        bets = synthesize_bets(rows)
        client = _StubClient({"m1": _open_market("m1", ["tok1", "tok2"])})
        resolved = resolve_bets(bets, client)
        assert not resolved[0].resolved
        assert resolved[0].pnl == 0.0
    finally:
        conn.close()


def test_summarize_groups_by_wallet_and_conviction(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _insert_signal(conn, wallet="surfandturf", asset_id="t1", market_slug="m1",
                       current_price=0.40, conviction_discount=1.0)
        _insert_signal(conn, wallet="surfandturf", asset_id="t2", market_slug="m2",
                       current_price=0.50, conviction_discount=0.5)
        _insert_signal(conn, wallet="bossoskil1", asset_id="t3", market_slug="m3",
                       current_price=0.30, conviction_discount=1.0)
        rows = collect_signals(conn, since_days=30)
        bets = synthesize_bets(rows, weight_by_conviction=False)
        client = _StubClient({
            "m1": _resolved_market("m1", ["t1", "t1b"], 0),  # win
            "m2": _resolved_market("m2", ["t2", "t2b"], 1),  # loss
            "m3": _resolved_market("m3", ["t3", "t3b"], 0),  # win
        })
        resolved = resolve_bets(bets, client)
        s = summarize(len(rows), resolved)
        assert s.signals_total == 3
        assert s.bets_resolved == 3
        assert s.by_wallet["surfandturf"]["wins"] == 1
        assert s.by_wallet["surfandturf"]["losses"] == 1
        assert s.by_wallet["bossoskil1"]["wins"] == 1
        assert "full" in s.by_bucket
        assert "discount-floor" in s.by_bucket
    finally:
        conn.close()
