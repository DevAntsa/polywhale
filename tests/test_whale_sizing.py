"""Tests for Kelly-fractional sizing."""

import time
from pathlib import Path

from polywhale.db import connect, run_migrations
from polywhale.whale_sizing import (
    CAP_PER_BET,
    EXPLORE_STAKE_PCT,
    MAX_OPEN_POSITIONS,
    MAX_PORTFOLIO_DEPLOY_PCT,
    check_portfolio_guards,
    compute_kelly_stake,
    whale_pnl_stats,
)


def _insert_closed(conn, *, wallet: str, pnl: float, cost: float = 40.0):
    cur = conn.execute(
        "INSERT INTO whale_signals(wallet, signal_type, asset_id, market_slug, "
        "prev_captured_at, latest_captured_at, detected_at) "
        "VALUES (?, 'new_position', ?, ?, 1, 2, ?)",
        (wallet, f"asset_{pnl}_{cost}_{time.time_ns()}", "m1", int(time.time())),
    )
    sig_id = cur.lastrowid
    conn.execute(
        "INSERT INTO poly_paper_bets(source, source_ref_id, market_slug, "
        "token_id, side, entry_price, size_shares, cost_usd, placed_at, "
        "settled_at, payout_per_share, pnl_usd) "
        "VALUES ('whale_copy', ?, 'm1', ?, 'YES', 0.4, 100, ?, 1, 100, 0.5, ?)",
        (sig_id, f"t_{time.time_ns()}", cost, pnl),
    )
    conn.commit()


def test_whale_pnl_stats_computes_per_dollar(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        # 3 trades: +$8 / $40 cost, -$4 / $40, +$2 / $40 → per_dollar: 0.2, -0.1, 0.05
        _insert_closed(conn, wallet="0xw", pnl=8.0, cost=40.0)
        _insert_closed(conn, wallet="0xw", pnl=-4.0, cost=40.0)
        _insert_closed(conn, wallet="0xw", pnl=2.0, cost=40.0)
        mu, sigma2, n = whale_pnl_stats(conn, "0xw")
        assert n == 3
        # mean of [0.2, -0.1, 0.05] = 0.05
        assert abs(mu - 0.05) < 1e-4
        assert sigma2 > 0
    finally:
        conn.close()


def test_kelly_exploration_for_small_sample(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        for _ in range(5):
            _insert_closed(conn, wallet="0xw", pnl=10.0)
        r = compute_kelly_stake(conn, "0xw", bankroll_usd=2000.0)
        # Below 10 trades → 0.5% exploration stake = $10
        assert abs(r.stake_usd - 2000.0 * EXPLORE_STAKE_PCT) < 0.01
        assert "exploration" in r.reason
    finally:
        conn.close()


def test_kelly_drops_clearly_negative_whale(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        # 30 trades all -$2 — clearly losing per dollar
        for _ in range(30):
            _insert_closed(conn, wallet="0xloser", pnl=-2.0)
        r = compute_kelly_stake(conn, "0xloser", bankroll_usd=2000.0)
        assert r.stake_usd == 0.0
        assert r.skipped is True
        assert "drop" in r.reason
    finally:
        conn.close()


def test_kelly_drops_zero_variance(tmp_path: Path) -> None:
    """All trades identical → variance 0 → can't size on Kelly. Drop."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        for _ in range(30):
            _insert_closed(conn, wallet="0xflat", pnl=0.0)
        r = compute_kelly_stake(conn, "0xflat", bankroll_usd=2000.0)
        assert r.skipped is True
    finally:
        conn.close()


def test_kelly_sizes_winner_with_full_sample(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        # 30 trades: alternating +$8 / -$4 → mean +$2 per trade, per-dollar 0.05
        for i in range(30):
            _insert_closed(conn, wallet="0xwinner", pnl=8.0 if i % 2 == 0 else -4.0)
        r = compute_kelly_stake(conn, "0xwinner", bankroll_usd=2000.0)
        # Should be a non-trivial positive stake, bounded by CAP_PER_BET
        assert r.stake_usd > 0
        assert r.stake_usd <= 2000.0 * CAP_PER_BET + 0.01
        assert "kelly" in r.reason
    finally:
        conn.close()


def test_kelly_respects_cap_per_bet(tmp_path: Path) -> None:
    """Even a huge edge gets capped at CAP_PER_BET."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        # 30 trades all positive — massive edge would blow past cap
        for _ in range(30):
            _insert_closed(conn, wallet="0xunicorn", pnl=20.0)
        r = compute_kelly_stake(conn, "0xunicorn", bankroll_usd=2000.0)
        assert r.stake_usd <= 2000.0 * CAP_PER_BET + 0.01
    finally:
        conn.close()


def test_kelly_shrinkage_between_low_and_high(tmp_path: Path) -> None:
    """At n=20 with a clear edge + low variance, Kelly returns positive shrunk stake."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        # 20 trades with strong consistent edge: 14 at +$3, 6 at +$1.5
        # → mean per dollar ≈ 0.064, low variance → narrow CI, well above -fees
        for i in range(20):
            _insert_closed(conn, wallet="0xw", pnl=3.0 if i < 14 else 1.5)
        r = compute_kelly_stake(conn, "0xw", bankroll_usd=2000.0)
        assert r.stake_usd > 0
        assert r.stake_usd <= 2000.0 * CAP_PER_BET + 0.01
        assert "kelly" in r.reason
        assert r.sample_size == 20
    finally:
        conn.close()


def test_check_portfolio_guards_max_open(tmp_path: Path) -> None:
    """(MAX_OPEN_POSITIONS+1)th open position should be rejected."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        for i in range(MAX_OPEN_POSITIONS):
            conn.execute(
                "INSERT INTO poly_paper_bets(source, market_slug, token_id, side, "
                "entry_price, size_shares, cost_usd, placed_at) "
                "VALUES ('whale_copy', ?, ?, 'YES', 0.4, 100, 40, 1)",
                (f"m{i}", f"t{i}"),
            )
        conn.commit()
        ok, reason = check_portfolio_guards(
            conn, proposed_stake=40.0, bankroll_usd=2000.0,
            market_slug="m_new", outcome="Yes",
        )
        assert ok is False
        assert "max_open_positions" in reason
    finally:
        conn.close()


def test_check_portfolio_guards_dedup_same_market(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        conn.execute(
            "INSERT INTO poly_paper_bets(source, market_slug, token_id, side, "
            "entry_price, size_shares, cost_usd, placed_at, outcome_title) "
            "VALUES ('whale_copy', 'm1', 't1', 'YES', 0.4, 100, 40, 1, 'Yankees')"
        )
        conn.commit()
        ok, reason = check_portfolio_guards(
            conn, proposed_stake=40.0, bankroll_usd=2000.0,
            market_slug="m1", outcome="Yankees",
        )
        assert ok is False
        assert "dedup" in reason
    finally:
        conn.close()


def test_check_portfolio_guards_deployment_cap(tmp_path: Path) -> None:
    """Deploying past MAX_PORTFOLIO_DEPLOY_PCT triggers cap."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        bankroll = 2000.0
        cap_usd = bankroll * MAX_PORTFOLIO_DEPLOY_PCT
        already_deployed = cap_usd - 20.0
        conn.execute(
            "INSERT INTO poly_paper_bets(source, market_slug, token_id, side, "
            "entry_price, size_shares, cost_usd, placed_at) "
            "VALUES ('whale_copy', 'm1', 't1', 'YES', 0.4, 100, ?, 1)",
            (already_deployed,),
        )
        conn.commit()
        ok, reason = check_portfolio_guards(
            conn, proposed_stake=30.0, bankroll_usd=bankroll,
            market_slug="m2", outcome="Yes",
        )
        assert ok is False
        assert "portfolio_deploy_cap" in reason
    finally:
        conn.close()
