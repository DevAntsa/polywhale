import time
from pathlib import Path

from polywhale.copy_trader import (
    close_copy_bet,
    copy_trade_stats,
    find_closed_copy_bet_by_exit_signal,
    find_open_copy_bet_for_signal,
    place_copy_bet,
    process_copy_trades,
)
from polywhale.db import connect, run_migrations


def _insert_signal(
    conn,
    *,
    wallet: str,
    asset_id: str,
    signal_type: str,
    current_price: float | None,
    market_slug: str = "m1",
    outcome: str = "Yes",
    conviction_discount: float | None = 1.0,
    new_size: float | None = 100_000,
    old_size: float | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO whale_signals(
            wallet, signal_type, asset_id, market_slug, outcome,
            old_size, new_size, current_price, prev_captured_at,
            latest_captured_at, detected_at, conviction_discount
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 2, ?, ?)
        """,
        (
            wallet, signal_type, asset_id, market_slug, outcome,
            old_size, new_size, current_price, int(time.time()), conviction_discount,
        ),
    )
    conn.commit()
    return cur.lastrowid or 0


def _row(conn, signal_id: int):
    return conn.execute(
        "SELECT * FROM whale_signals WHERE signal_id = ?", (signal_id,)
    ).fetchone()


def test_place_copy_bet_sizes_by_bankroll_pct(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        sig_id = _insert_signal(conn, wallet="0xw", asset_id="t1",
                                signal_type="new_position", current_price=0.40)
        sig_row = _row(conn, sig_id)
        assert place_copy_bet(conn, sig_row, bankroll_usd=2000.0, stake_pct=0.02)
        bet = find_open_copy_bet_for_signal(conn, sig_id)
        assert bet is not None
        # 2000 * 0.02 * 1.0 (conviction) = 40 stake; shares = 40/0.40 = 100
        assert abs(bet["cost_usd"] - 40.0) < 1e-3
        assert abs(bet["size_shares"] - 100.0) < 1e-3
        assert bet["source"] == "whale_copy"
        assert bet["source_ref_id"] == sig_id
    finally:
        conn.close()


def test_place_copy_bet_weights_by_conviction(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        sig_id = _insert_signal(conn, wallet="0xw", asset_id="t1",
                                signal_type="new_position", current_price=0.40,
                                conviction_discount=0.5)
        sig_row = _row(conn, sig_id)
        place_copy_bet(conn, sig_row, bankroll_usd=2000.0, stake_pct=0.02)
        bet = find_open_copy_bet_for_signal(conn, sig_id)
        # 2000 * 0.02 * 0.5 = 20 stake
        assert abs(bet["cost_usd"] - 20.0) < 1e-3
    finally:
        conn.close()


def test_place_copy_bet_idempotent(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        sig_id = _insert_signal(conn, wallet="0xw", asset_id="t1",
                                signal_type="new_position", current_price=0.40)
        sig_row = _row(conn, sig_id)
        assert place_copy_bet(conn, sig_row, bankroll_usd=2000.0, stake_pct=0.02)
        assert not place_copy_bet(conn, sig_row, bankroll_usd=2000.0, stake_pct=0.02)
        n = conn.execute("SELECT COUNT(*) FROM poly_paper_bets").fetchone()[0]
        assert n == 1
    finally:
        conn.close()


def test_place_copy_bet_skips_bad_price(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        for bad_price in (None, 0.0, 1.0):
            sig_id = _insert_signal(conn, wallet="0xw", asset_id=f"t-{bad_price}",
                                    signal_type="new_position", current_price=bad_price)
            sig_row = conn.execute(
                "SELECT * FROM whale_signals WHERE signal_id = ?", (sig_id,)
            ).fetchone()
            assert not place_copy_bet(conn, sig_row, bankroll_usd=2000.0, stake_pct=0.02)
    finally:
        conn.close()


def test_close_copy_bet_computes_pnl(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        # Whale opens at 0.40, we copy
        entry_sig = _insert_signal(conn, wallet="0xw", asset_id="t1",
                                   signal_type="new_position", current_price=0.40)
        entry_row = _row(conn, entry_sig)
        place_copy_bet(conn, entry_row, bankroll_usd=2000.0, stake_pct=0.02)
        # Whale exits at 0.55
        exit_sig = _insert_signal(conn, wallet="0xw", asset_id="t1",
                                  signal_type="closed_position", current_price=0.55,
                                  new_size=0, old_size=100_000)
        exit_row = _row(conn, exit_sig)
        pnl = close_copy_bet(conn, exit_row)
        assert pnl is not None
        # shares=100 at 0.40 entry, exit 0.55 -> pnl = (0.55 - 0.40) * 100 = 15
        assert abs(pnl - 15.0) < 1e-3
        # Bet now linked to the exit signal
        bet = find_closed_copy_bet_by_exit_signal(conn, exit_sig)
        assert bet is not None
        assert bet["closed_by_signal_id"] == exit_sig
        assert bet["resolved_outcome"] == "closed_early"
        assert bet["settled_at"] is not None
    finally:
        conn.close()


def test_close_copy_bet_returns_none_if_no_open_bet(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        # Bot wasn't running for the entry; only the exit fires
        exit_sig = _insert_signal(conn, wallet="0xw", asset_id="t1",
                                  signal_type="closed_position", current_price=0.55,
                                  new_size=0, old_size=100_000)
        exit_row = _row(conn, exit_sig)
        assert close_copy_bet(conn, exit_row) is None
    finally:
        conn.close()


def test_process_copy_trades_handles_mixed_signals(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _insert_signal(conn, wallet="0xa", asset_id="ta", signal_type="new_position",
                       current_price=0.30)
        _insert_signal(conn, wallet="0xb", asset_id="tb", signal_type="new_position",
                       current_price=0.50)
        result = process_copy_trades(conn, bankroll_usd=2000.0, stake_pct=0.02)
        assert result["opened"] == 2
        assert result["closed"] == 0

        # Now process EXIT for one of them
        _insert_signal(conn, wallet="0xa", asset_id="ta", signal_type="closed_position",
                       current_price=0.40, new_size=0, old_size=100_000)
        result2 = process_copy_trades(conn, bankroll_usd=2000.0, stake_pct=0.02)
        assert result2["opened"] == 0
        assert result2["closed"] == 1
        # Entry 0.30, exit 0.40, shares 40/0.30 ~= 133.33 -> pnl = 0.1 * 133.33 ~= 13.33
        assert abs(result2["realized_pnl"] - 13.33) < 0.5
    finally:
        conn.close()


def test_copy_trade_stats_aggregates(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        # 2 opens, 1 close (win), 1 close (loss)
        for wallet, asset, entry, exit_p in [
            ("0xa", "t1", 0.30, 0.50),
            ("0xb", "t2", 0.60, 0.40),
        ]:
            entry_sig = _insert_signal(conn, wallet=wallet, asset_id=asset,
                                       signal_type="new_position", current_price=entry)
            place_copy_bet(conn, _row(conn, entry_sig), bankroll_usd=2000.0, stake_pct=0.02)
            exit_sig = _insert_signal(conn, wallet=wallet, asset_id=asset,
                                      signal_type="closed_position", current_price=exit_p,
                                      new_size=0, old_size=100_000)
            close_copy_bet(conn, _row(conn, exit_sig))
        # Open one more
        sig3 = _insert_signal(conn, wallet="0xc", asset_id="t3", signal_type="new_position",
                              current_price=0.40)
        place_copy_bet(conn, _row(conn, sig3), bankroll_usd=2000.0, stake_pct=0.02)

        stats = copy_trade_stats(conn)
        assert stats["open_positions"] == 1
        assert stats["closed_positions"] == 2
        assert stats["wins"] == 1
        assert stats["losses"] == 1
        assert stats["win_rate_pct"] == 50.0
    finally:
        conn.close()
