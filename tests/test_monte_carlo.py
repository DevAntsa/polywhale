import time
from pathlib import Path

import pytest

from polywhale.db import connect, run_migrations
from polywhale.monte_carlo import (
    fetch_pnls,
    max_drawdown,
    simulate_aggregated,
    simulate_per_whale,
)


def _insert_closed_bet(
    conn,
    *,
    wallet: str = "0xw",
    pnl: float,
    placed_offset: int = 100,
    settled_offset: int = 200,
):
    now = int(time.time())
    cur = conn.execute(
        "INSERT INTO whale_signals(wallet, signal_type, asset_id, market_slug, "
        "prev_captured_at, latest_captured_at, detected_at) "
        "VALUES (?, 'new_position', ?, ?, 1, 2, ?)",
        (wallet, f"asset_{pnl}_{now}", f"market_{pnl}", now - placed_offset),
    )
    sig_id = cur.lastrowid
    conn.execute(
        "INSERT INTO poly_paper_bets(source, source_ref_id, market_slug, "
        "token_id, side, entry_price, size_shares, cost_usd, placed_at, "
        "settled_at, payout_per_share, pnl_usd) "
        "VALUES ('whale_copy', ?, ?, ?, 'YES', 0.4, 100, 40, ?, ?, 0.5, ?)",
        (sig_id, f"market_{pnl}", f"asset_{pnl}_{now}",
         now - placed_offset, now - settled_offset, pnl),
    )
    conn.commit()


def test_max_drawdown_basic() -> None:
    # Run up to +10, down to -5, back up. Peak was 10, trough 0 → dd=10
    assert max_drawdown([5, 5, -5, -5, 4]) == 10.0


def test_max_drawdown_all_positive_returns_zero() -> None:
    assert max_drawdown([1, 2, 3, 4]) == 0.0


def test_max_drawdown_all_negative() -> None:
    # Continuously declining → DD == abs total
    assert max_drawdown([-1, -2, -3]) == 6.0


def test_fetch_pnls_filters_to_settled(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _insert_closed_bet(conn, pnl=10.0)
        _insert_closed_bet(conn, pnl=-5.0)
        # Unsettled bet must not appear
        conn.execute(
            "INSERT INTO poly_paper_bets(source, market_slug, token_id, side, "
            "entry_price, size_shares, cost_usd, placed_at) "
            "VALUES ('whale_copy', 'mX', 'tX', 'YES', 0.5, 100, 50, 1)"
        )
        conn.commit()
        pnls = fetch_pnls(conn)
        assert sorted(pnls) == [-5.0, 10.0]
    finally:
        conn.close()


def test_simulate_aggregated_all_positive_history(tmp_path: Path) -> None:
    """If every historical trade was a winner, prob_positive ~ 1.0."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        for i in range(20):
            _insert_closed_bet(conn, pnl=5.0 + i)
        result = simulate_aggregated(conn, horizon_days=7, samples=500, seed=42)
        assert result.prob_positive >= 0.99
        assert result.mean_pnl > 0
        # Drawdown on monotone positive series should be 0
        assert result.median_drawdown == 0.0
    finally:
        conn.close()


def test_simulate_aggregated_all_negative_history(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        for i in range(20):
            _insert_closed_bet(conn, pnl=-5.0 - i)
        result = simulate_aggregated(conn, horizon_days=7, samples=500, seed=42)
        assert result.prob_positive <= 0.01
        assert result.mean_pnl < 0
    finally:
        conn.close()


def test_simulate_aggregated_reproducible_with_seed(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        for i in range(20):
            _insert_closed_bet(conn, pnl=(i - 10) * 1.5)
        r1 = simulate_aggregated(conn, samples=1000, seed=42)
        r2 = simulate_aggregated(conn, samples=1000, seed=42)
        assert r1.mean_pnl == r2.mean_pnl
        assert r1.p5 == r2.p5
        assert r1.p95 == r2.p95
    finally:
        conn.close()


def test_simulate_aggregated_raises_on_empty(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        with pytest.raises(ValueError, match="no closed"):
            simulate_aggregated(conn, samples=100)
    finally:
        conn.close()


def test_simulate_per_whale_separates_by_wallet(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        # Winner whale: 20 trades all +$5
        for _ in range(20):
            _insert_closed_bet(conn, wallet="0xwinner", pnl=5.0)
        # Loser whale: 20 trades all -$3
        for _ in range(20):
            _insert_closed_bet(conn, wallet="0xloser", pnl=-3.0)
        result = simulate_per_whale(conn, samples=500, seed=7)
        # Should have both whales in breakdown
        assert "0xwinner" in result.per_whale_breakdown
        assert "0xloser" in result.per_whale_breakdown
        # Winner should net positive, loser negative
        assert result.per_whale_breakdown["0xwinner"] > 0
        assert result.per_whale_breakdown["0xloser"] < 0
    finally:
        conn.close()


def test_simulate_per_whale_skips_inactive_whales(tmp_path: Path) -> None:
    """A whale with 0 trades shouldn't appear in the breakdown."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        for _ in range(15):
            _insert_closed_bet(conn, wallet="0xactive", pnl=2.0)
        result = simulate_per_whale(conn, samples=200, seed=1)
        assert "0xactive" in result.per_whale_breakdown
        assert "0xnonexistent" not in result.per_whale_breakdown
    finally:
        conn.close()
