"""Tests for position reconstruction + historical backtest."""

import time
from pathlib import Path

from polywhale.db import connect, run_migrations
from polywhale.historical_backtest import reconstruct_positions_for_wallet


class _StubClient:
    """get_market is the only method used during reconstruction (for still-open
    positions via get_or_fetch_resolution). Most tests don't need it."""
    def get_market(self, slug):
        return None

    @property
    def _data(self):
        return None


def _insert_event(conn, **kw):
    defaults = {
        "wallet": "0xw", "timestamp": 100, "type": "TRADE",
        "condition_id": "c1", "asset": "a1", "side": "BUY",
        "price": 0.4, "size": 100, "usdc_size": 40,
        "outcome": "Yes", "outcome_index": 0,
        "market_slug": "m1", "title": "Test",
        "transaction_hash": "tx_" + str(kw.get("timestamp", 100)) + str(kw.get("size", 100)),
        "fetched_at": int(time.time()),
    }
    defaults.update(kw)
    conn.execute(
        """
        INSERT INTO whale_activity_history(
            wallet, timestamp, type, condition_id, asset, side, price, size,
            usdc_size, outcome, outcome_index, market_slug, title,
            transaction_hash, fetched_at
        ) VALUES (
            :wallet, :timestamp, :type, :condition_id, :asset, :side, :price, :size,
            :usdc_size, :outcome, :outcome_index, :market_slug, :title,
            :transaction_hash, :fetched_at
        )
        """,
        defaults,
    )
    conn.commit()


def test_buy_then_sell_creates_one_closed_episode(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _insert_event(conn, timestamp=100, side="BUY", price=0.40, size=100)
        _insert_event(conn, timestamp=200, side="SELL", price=0.55, size=100)
        episodes = reconstruct_positions_for_wallet(conn, _StubClient(), "0xw")
        assert len(episodes) == 1
        ep = episodes[0]
        assert ep.resolution_status == "sold"
        # entry vwap 0.40, exit vwap 0.55, shares 100 → pnl = 15
        assert abs(ep.pnl_usd - 15.0) < 0.01
    finally:
        conn.close()


def test_multiple_buys_compute_vwap(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _insert_event(conn, timestamp=100, side="BUY", price=0.40, size=100)
        _insert_event(conn, timestamp=110, side="BUY", price=0.50, size=100)
        _insert_event(conn, timestamp=200, side="SELL", price=0.55, size=200)
        episodes = reconstruct_positions_for_wallet(conn, _StubClient(), "0xw")
        assert len(episodes) == 1
        ep = episodes[0]
        # entry vwap = (40 + 50) / 200 = 0.45
        assert abs(ep.entry_vwap - 0.45) < 1e-4
        # exit vwap = 0.55
        assert abs(ep.exit_vwap - 0.55) < 1e-4
        # pnl = (0.55 - 0.45) * 200 = 20
        assert abs(ep.pnl_usd - 20.0) < 0.01
    finally:
        conn.close()


def test_redeem_marks_episode_as_won(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _insert_event(conn, timestamp=100, side="BUY", price=0.40, size=100)
        # REDEEM at $1.0 per share → received 100 USDC for 100 shares
        _insert_event(
            conn, timestamp=200, type="REDEEM", side="",
            price=0, size=100, usdc_size=100,
        )
        episodes = reconstruct_positions_for_wallet(conn, _StubClient(), "0xw")
        assert len(episodes) == 1
        assert episodes[0].resolution_status == "won"
        # pnl = 100 - 40 = 60
        assert abs(episodes[0].pnl_usd - 60.0) < 0.01
    finally:
        conn.close()


def test_two_separate_episodes_on_same_market(tmp_path: Path) -> None:
    """BUY, SELL all → episode 1 closed. BUY, SELL → episode 2."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _insert_event(conn, timestamp=100, side="BUY", price=0.40, size=50)
        _insert_event(conn, timestamp=110, side="SELL", price=0.50, size=50)
        _insert_event(conn, timestamp=200, side="BUY", price=0.30, size=80)
        _insert_event(conn, timestamp=210, side="SELL", price=0.45, size=80)
        episodes = reconstruct_positions_for_wallet(conn, _StubClient(), "0xw")
        assert len(episodes) == 2
        # first: (0.50 - 0.40) * 50 = 5
        # second: (0.45 - 0.30) * 80 = 12
        pnls = sorted(e.pnl_usd for e in episodes)
        assert abs(pnls[0] - 5.0) < 0.01
        assert abs(pnls[1] - 12.0) < 0.01
    finally:
        conn.close()


def test_open_position_emitted_with_no_pnl(tmp_path: Path) -> None:
    """BUY only, no exit yet — emits episode with status='open', pnl=None."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _insert_event(conn, timestamp=100, side="BUY", price=0.40, size=100)
        episodes = reconstruct_positions_for_wallet(conn, _StubClient(), "0xw")
        assert len(episodes) == 1
        assert episodes[0].resolution_status == "open"
        assert episodes[0].pnl_usd is None
        assert episodes[0].exit_vwap is None
    finally:
        conn.close()


def test_different_assets_get_separate_episodes(tmp_path: Path) -> None:
    """Buying YES and NO on the same market produces two episodes."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _insert_event(conn, timestamp=100, asset="yes_token", side="BUY", price=0.40, size=100)
        _insert_event(conn, timestamp=200, asset="yes_token", side="SELL", price=0.55, size=100)
        _insert_event(conn, timestamp=150, asset="no_token", side="BUY", price=0.60, size=100)
        _insert_event(conn, timestamp=250, asset="no_token", side="SELL", price=0.45, size=100)
        episodes = reconstruct_positions_for_wallet(conn, _StubClient(), "0xw")
        assert len(episodes) == 2
        assets = sorted(e.asset for e in episodes)
        assert assets == ["no_token", "yes_token"]
    finally:
        conn.close()


def test_fee_pct_reduces_pnl(tmp_path: Path) -> None:
    """A 1% fee on entry+exit notional should reduce pnl correspondingly."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _insert_event(conn, timestamp=100, side="BUY", price=0.40, size=100)
        _insert_event(conn, timestamp=200, side="SELL", price=0.55, size=100)
        episodes = reconstruct_positions_for_wallet(conn, _StubClient(), "0xw", fee_pct=0.01)
        # gross: 15, fee = (40 + 55) * 0.01 = 0.95, net = 14.05
        assert abs(episodes[0].pnl_usd - 14.05) < 0.01
        assert abs(episodes[0].fee_paid - 0.95) < 0.01
    finally:
        conn.close()
