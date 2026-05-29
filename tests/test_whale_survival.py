"""Tests for the survival-score module (Cycle 3 research, 2026-05-29)."""

from pathlib import Path

from polywhale.db import connect, run_migrations
from polywhale.whale_refresh import upsert_manual
from polywhale.whale_survival import (
    HARD_DROP_SCORE,
    SIZE_CUT_SCORE,
    compute_survival_score,
    persist_survival_score,
    recompute_all,
    survival_tier,
)


def _insert_settled_bet(
    conn,
    *,
    wallet: str,
    asset_id: str,
    pnl: float,
    market_slug: str = "nba-test-2026",
    placed_at: int = 1000,
    settled_at: int = 2000,
) -> None:
    sig = conn.execute(
        "INSERT INTO whale_signals(wallet, signal_type, asset_id, market_slug, "
        "prev_captured_at, latest_captured_at, detected_at) "
        "VALUES (?, 'new_position', ?, ?, 1, 2, ?)",
        (wallet, asset_id, market_slug, placed_at),
    )
    sig_id = sig.lastrowid
    conn.execute(
        "INSERT INTO poly_paper_bets(source, source_ref_id, market_slug, token_id, "
        "side, entry_price, size_shares, cost_usd, placed_at, settled_at, "
        "pnl_usd, resolved_outcome) "
        "VALUES ('whale_copy', ?, ?, ?, 'YES', 0.5, 20, 10, ?, ?, ?, 'closed_early')",
        (sig_id, market_slug, asset_id, placed_at, settled_at, pnl),
    )
    conn.commit()


def test_compute_survival_score_zero_when_no_trades(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        upsert_manual(conn, wallet="0xnone")
        b = compute_survival_score(conn, "0xnone")
        # f_maker default (0.5 → 25 * 0.5/0.6 = 20.83), f_breadth at 0 categories
        # = 20 * max(0, 1 - 4/4) = 0, f_skew default 7.5, f_recovery 0, f_sample 0
        assert b.n_resolved == 0
        assert b.f_sample == 0.0
        assert b.f_breadth == 0.0
        # maker_share default 0.5 → f_maker = 25 * 0.5/0.6 ≈ 20.83
        assert abs(b.f_maker - 25 * 0.5 / 0.6) < 0.01
    finally:
        conn.close()


def test_compute_survival_score_high_n_good_recovery(tmp_path: Path) -> None:
    """A wallet with 50 settled trades and positive cum PnL should score high."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        upsert_manual(conn, wallet="0xpro")
        # 50 bets, half wins of $5, half losses of $2 → cum +$75, max DD shallow
        for i in range(50):
            pnl = 5.0 if i % 2 == 0 else -2.0
            _insert_settled_bet(
                conn,
                wallet="0xpro",
                asset_id=f"t{i}",
                market_slug=f"nba-{i}" if i < 25 else f"nfl-{i}",
                pnl=pnl,
                placed_at=1000 + i,
                settled_at=2000 + i,
            )
        b = compute_survival_score(conn, "0xpro", maker_share=0.6)
        assert b.n_resolved == 50
        assert b.f_sample == 25.0  # saturated
        assert b.f_maker == 25.0  # saturated at 0.6
        assert b.survival_score >= SIZE_CUT_SCORE  # full-size tier
    finally:
        conn.close()


def test_survival_tier_probation_when_low_n(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        upsert_manual(conn, wallet="0xprob")
        for i in range(5):
            _insert_settled_bet(
                conn, wallet="0xprob", asset_id=f"t{i}", pnl=2.0,
                placed_at=1000 + i, settled_at=2000 + i,
            )
        b = compute_survival_score(conn, "0xprob")
        assert survival_tier(b) == "probation"
    finally:
        conn.close()


def test_survival_tier_drop_candidate_when_low_score_high_n(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        upsert_manual(conn, wallet="0xbad")
        # 40 bets, all losers of $-3 → big negative recovery, sample saturated
        for i in range(40):
            _insert_settled_bet(
                conn, wallet="0xbad", asset_id=f"t{i}", pnl=-3.0,
                placed_at=1000 + i, settled_at=2000 + i,
            )
        b = compute_survival_score(conn, "0xbad", maker_share=0.0)
        # f_sample 25, f_maker 0, f_breadth=0 (1 category), f_recovery 0, f_skew default
        assert b.survival_score < HARD_DROP_SCORE
        assert survival_tier(b) == "drop-candidate"
    finally:
        conn.close()


def test_persist_survival_score_writes_columns(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        upsert_manual(conn, wallet="0xp1")
        for i in range(10):
            _insert_settled_bet(
                conn, wallet="0xp1", asset_id=f"t{i}", pnl=1.0,
                placed_at=1000 + i, settled_at=2000 + i,
            )
        b = compute_survival_score(conn, "0xp1", maker_share=0.4)
        persist_survival_score(conn, b)
        row = conn.execute(
            "SELECT survival_score, survival_n_resolved, survival_maker_share "
            "FROM whale_watchlist WHERE wallet = '0xp1'"
        ).fetchone()
        assert row["survival_n_resolved"] == 10
        assert abs(row["survival_maker_share"] - 0.4) < 0.001
        assert row["survival_score"] is not None
    finally:
        conn.close()


def test_recompute_all_processes_active_only(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        upsert_manual(conn, wallet="0xactive")
        upsert_manual(conn, wallet="0xinactive")
        conn.execute(
            "UPDATE whale_watchlist SET active = 0 WHERE wallet = '0xinactive'"
        )
        conn.commit()
        results = recompute_all(conn)
        wallets_scored = {r.wallet for r in results}
        assert "0xactive" in wallets_scored
        assert "0xinactive" not in wallets_scored
    finally:
        conn.close()
