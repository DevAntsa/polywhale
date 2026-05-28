import time
from pathlib import Path

from polywhale.db import connect, run_migrations
from polywhale.whale_refresh import upsert_manual
from polywhale.whale_review import (
    REC_DROP,
    REC_DROP_DORMANT,
    REC_KEEP,
    REC_KEEP_BOOST,
    REC_WATCH,
    TIER_DROP,
    TIER_DROP_DORMANT,
    TIER_KEEP,
    TIER_KEEP_BOOST,
    TIER_WATCH,
    auto_drop,
    evaluate_all_active,
    evaluate_whale,
)


def _add_auto_wallet(conn, wallet: str, *, last_signal_age_days: float = 1.0) -> None:
    now = int(time.time())
    last_sig = now - int(last_signal_age_days * 86400)
    conn.execute(
        "INSERT INTO whale_watchlist(wallet, source, added_at, active, "
        "margin_pct, profit_usd, signals_30d, last_signal_at) "
        "VALUES (?, 'auto-margin', ?, 1, 4.0, 100000, 5, ?)",
        (wallet, now - 30 * 86400, last_sig),
    )
    conn.commit()


def _add_closed_bets(
    conn, wallet: str, *, count: int, pnl_each: float, settled_age_days: float = 1.0
) -> None:
    now = int(time.time())
    placed = now - 2 * 86400
    settled = now - int(settled_age_days * 86400)
    for i in range(count):
        cur = conn.execute(
            "INSERT INTO whale_signals(wallet, signal_type, asset_id, market_slug, "
            "prev_captured_at, latest_captured_at, detected_at) "
            "VALUES (?, 'new_position', ?, ?, 1, 2, ?)",
            (wallet, f"t{i}", f"m{i}", placed),
        )
        sig_id = cur.lastrowid
        conn.execute(
            "INSERT INTO poly_paper_bets(source, source_ref_id, market_slug, "
            "token_id, side, entry_price, size_shares, cost_usd, placed_at, "
            "settled_at, payout_per_share, pnl_usd) "
            "VALUES ('whale_copy', ?, ?, ?, 'YES', 0.4, 100, 40, ?, ?, 0.5, ?)",
            (sig_id, f"m{i}", f"t{i}", placed, settled, pnl_each),
        )
    conn.commit()


def test_evaluate_whale_proven_contributor(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _add_auto_wallet(conn, "0xa")
        _add_closed_bets(conn, "0xa", count=30, pnl_each=10.0)  # $300 over 30 trades
        rev = evaluate_whale(conn, "0xa", min_trades_to_judge=25, boost_threshold=200.0)
        assert rev is not None
        assert rev.tier == TIER_KEEP_BOOST
        assert rev.recommendation == REC_KEEP_BOOST
        assert rev.closed_trades_all == 30
        assert rev.realized_pnl_all == 300.0
    finally:
        conn.close()


def test_evaluate_whale_drops_on_loss(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _add_auto_wallet(conn, "0xb")
        _add_closed_bets(conn, "0xb", count=30, pnl_each=-2.0)  # -$60
        rev = evaluate_whale(conn, "0xb", min_trades_to_judge=25, loss_threshold=-30.0)
        assert rev.tier == TIER_DROP
        assert rev.recommendation == REC_DROP
        assert "net loss" in rev.reason
    finally:
        conn.close()


def test_evaluate_whale_drops_on_zero_alpha(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _add_auto_wallet(conn, "0xc")
        _add_closed_bets(conn, "0xc", count=30, pnl_each=0.05)  # ~$0/trade
        rev = evaluate_whale(
            conn, "0xc", min_trades_to_judge=25, zero_pnl_epsilon=0.20
        )
        assert rev.tier == TIER_DROP
        assert rev.recommendation == REC_DROP
        assert "no captured alpha" in rev.reason
    finally:
        conn.close()


def test_evaluate_whale_watch_when_sample_small(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _add_auto_wallet(conn, "0xd")
        _add_closed_bets(conn, "0xd", count=5, pnl_each=-10.0)  # losing but small sample
        rev = evaluate_whale(conn, "0xd", min_trades_to_judge=25)
        assert rev.tier == TIER_WATCH
        assert rev.recommendation == REC_WATCH
        assert "too early" in rev.reason
    finally:
        conn.close()


def test_evaluate_whale_drops_dormant_with_no_track_record(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _add_auto_wallet(conn, "0xe", last_signal_age_days=30.0)  # dormant
        rev = evaluate_whale(conn, "0xe", max_quiet_days=21, min_trades_to_judge=25)
        assert rev.tier == TIER_DROP_DORMANT
        assert rev.recommendation == REC_DROP_DORMANT
    finally:
        conn.close()


def test_evaluate_whale_manual_never_auto_dropped(tmp_path: Path) -> None:
    """Manual entries get tier flagged but recommendation downgraded to watch."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        upsert_manual(conn, wallet="0xmanual")
        _add_closed_bets(conn, "0xmanual", count=30, pnl_each=-2.0)
        rev = evaluate_whale(conn, "0xmanual", min_trades_to_judge=25)
        # tier still shows the diagnosis...
        assert rev.tier == TIER_DROP
        # ...but recommendation is watch
        assert rev.recommendation == REC_WATCH
        assert "manual" in rev.reason
    finally:
        conn.close()


def test_evaluate_whale_keep_when_positive(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _add_auto_wallet(conn, "0xf")
        _add_closed_bets(conn, "0xf", count=30, pnl_each=2.0)  # +$60
        rev = evaluate_whale(
            conn, "0xf", min_trades_to_judge=25, boost_threshold=200.0
        )
        assert rev.tier == TIER_KEEP
        assert rev.recommendation == REC_KEEP
    finally:
        conn.close()


def test_auto_drop_deactivates_droppable_entries(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _add_auto_wallet(conn, "0xloser")
        _add_closed_bets(conn, "0xloser", count=30, pnl_each=-2.0)
        _add_auto_wallet(conn, "0xwinner")
        _add_closed_bets(conn, "0xwinner", count=30, pnl_each=10.0)
        reviews = evaluate_all_active(conn, min_trades_to_judge=25)
        droppable = [r for r in reviews if r.recommendation in ("drop", "drop_dormant")]
        dropped = auto_drop(conn, droppable)
        assert "0xloser" in dropped
        assert "0xwinner" not in dropped
        # Verify state
        row = conn.execute(
            "SELECT active, deactivated_reason FROM whale_watchlist WHERE wallet = '0xloser'"
        ).fetchone()
        assert row["active"] == 0
        assert "review" in row["deactivated_reason"]
    finally:
        conn.close()


def test_evaluate_returns_none_for_unknown_wallet(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        assert evaluate_whale(conn, "0xnothere") is None
    finally:
        conn.close()
