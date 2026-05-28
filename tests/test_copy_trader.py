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


def test_place_copy_bet_uses_exploration_stake_for_unknown_whale(tmp_path: Path) -> None:
    """A whale with no prior closed bets gets the exploration stake (0.5% bankroll)."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        sig_id = _insert_signal(conn, wallet="0xw", asset_id="t1",
                                signal_type="new_position", current_price=0.40)
        sig_row = _row(conn, sig_id)
        assert place_copy_bet(conn, sig_row, bankroll_usd=2000.0, stake_pct=0.02)
        bet = find_open_copy_bet_for_signal(conn, sig_id)
        assert bet is not None
        # Exploration stake = 0.5% x $2000 = $10
        assert abs(bet["cost_usd"] - 10.0) < 0.5
        # Shares = $10 / 0.40 = 25
        assert abs(bet["size_shares"] - 25.0) < 1.0
        assert bet["source"] == "whale_copy"
        assert bet["source_ref_id"] == sig_id
    finally:
        conn.close()


def test_place_copy_bet_weights_by_conviction(tmp_path: Path) -> None:
    """Conviction discount halves the exploration stake."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        sig_id = _insert_signal(conn, wallet="0xw", asset_id="t1",
                                signal_type="new_position", current_price=0.40,
                                conviction_discount=0.5)
        sig_row = _row(conn, sig_id)
        place_copy_bet(conn, sig_row, bankroll_usd=2000.0, stake_pct=0.02)
        bet = find_open_copy_bet_for_signal(conn, sig_id)
        # 0.5% exploration x 0.5 conviction x $2000 = $5
        assert abs(bet["cost_usd"] - 5.0) < 0.5
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
        # exploration stake $10 → 25 shares at 0.40 → pnl = 0.15 x 25 = $3.75
        assert abs(pnl - 3.75) < 0.1
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


def test_added_signal_tops_up_existing_bet(tmp_path: Path) -> None:
    """ADDED signal on a market we already hold should top up the existing bet."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        # Open a NEW bet at 0.40
        new_sig = _insert_signal(conn, wallet="0xw", asset_id="t1",
                                 signal_type="new_position", current_price=0.40,
                                 market_slug="m1")
        place_copy_bet(conn, _row(conn, new_sig), bankroll_usd=2000.0, stake_pct=0.02)
        n_initial = conn.execute(
            "SELECT COUNT(*) FROM poly_paper_bets WHERE source = 'whale_copy'"
        ).fetchone()[0]
        assert n_initial == 1
        # Whale ADDS — should top up, not create a new row
        add_sig = _insert_signal(conn, wallet="0xw", asset_id="t1",
                                 signal_type="added_size", current_price=0.50,
                                 market_slug="m1", new_size=200_000, old_size=100_000)
        place_copy_bet(conn, _row(conn, add_sig), bankroll_usd=2000.0, stake_pct=0.02)
        n_after = conn.execute(
            "SELECT COUNT(*) FROM poly_paper_bets WHERE source = 'whale_copy'"
        ).fetchone()[0]
        assert n_after == 1  # still one row
        bet = conn.execute(
            "SELECT * FROM poly_paper_bets WHERE source = 'whale_copy'"
        ).fetchone()
        assert bet["add_count"] == 1
        # cost = original $10 + additional $10 = $20 (exploration stake at 0.5% of $2K)
        assert abs(float(bet["cost_usd"]) - 20.0) < 0.5
        assert float(bet["additions_total_usd"]) > 0
        # entry vwap is between 0.40 and 0.50
        assert 0.40 < float(bet["entry_price"]) < 0.50
    finally:
        conn.close()


def test_added_with_no_existing_bet_opens_new(tmp_path: Path) -> None:
    """If ADDED fires for a wallet+asset we have no open bet on, opens new bet."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        sig = _insert_signal(conn, wallet="0xw", asset_id="t1",
                             signal_type="added_size", current_price=0.40,
                             market_slug="m1", new_size=200_000, old_size=100_000)
        assert place_copy_bet(conn, _row(conn, sig), bankroll_usd=2000.0, stake_pct=0.02)
        bet = conn.execute(
            "SELECT * FROM poly_paper_bets WHERE source = 'whale_copy'"
        ).fetchone()
        # Normal new bet, not a top-up
        assert int(bet["add_count"] or 0) == 0
    finally:
        conn.close()


def test_topup_vwap_computation(tmp_path: Path) -> None:
    """VWAP must be the dollar-weighted average of original + addition."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        # Open at 0.40 with $10 → 25 shares
        new_sig = _insert_signal(conn, wallet="0xw", asset_id="t1",
                                 signal_type="new_position", current_price=0.40,
                                 market_slug="m1")
        place_copy_bet(conn, _row(conn, new_sig), bankroll_usd=2000.0, stake_pct=0.02)
        # Add at 0.50 with $10 → 20 more shares
        add_sig = _insert_signal(conn, wallet="0xw", asset_id="t1",
                                 signal_type="added_size", current_price=0.50,
                                 market_slug="m1", new_size=200_000, old_size=100_000)
        place_copy_bet(conn, _row(conn, add_sig), bankroll_usd=2000.0, stake_pct=0.02)
        bet = conn.execute(
            "SELECT entry_price, size_shares, cost_usd FROM poly_paper_bets "
            "WHERE source = 'whale_copy'"
        ).fetchone()
        # cost = $20, shares = 25 + 20 = 45, vwap = 20/45 ~= 0.4444
        assert abs(float(bet["cost_usd"]) - 20.0) < 0.5
        assert abs(float(bet["size_shares"]) - 45.0) < 1.0
        assert abs(float(bet["entry_price"]) - 0.4444) < 0.02
    finally:
        conn.close()


def test_process_copy_trades_handles_mixed_signals(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        # Different market_slugs so dedup doesn't reject the second one
        _insert_signal(conn, wallet="0xa", asset_id="ta", signal_type="new_position",
                       current_price=0.30, market_slug="market_a")
        _insert_signal(conn, wallet="0xb", asset_id="tb", signal_type="new_position",
                       current_price=0.50, market_slug="market_b")
        result = process_copy_trades(conn, bankroll_usd=2000.0, stake_pct=0.02)
        assert result["opened"] == 2
        assert result["closed"] == 0

        # Now process EXIT for one of them
        _insert_signal(conn, wallet="0xa", asset_id="ta", signal_type="closed_position",
                       current_price=0.40, new_size=0, old_size=100_000,
                       market_slug="market_a")
        result2 = process_copy_trades(conn, bankroll_usd=2000.0, stake_pct=0.02)
        assert result2["opened"] == 0
        assert result2["closed"] == 1
        # Exploration stake $10 / 0.30 ~= 33.33 shares, pnl = 0.10 x 33.33 ~= $3.33
        assert abs(result2["realized_pnl"] - 3.33) < 0.5
    finally:
        conn.close()


def test_place_copy_bet_refuses_when_portfolio_cap_full(tmp_path: Path) -> None:
    """New bets are skipped once 25% portfolio deployment cap is engaged."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        # Bankroll $2000 → 25% cap = $500. Pre-populate $495 already deployed.
        # Exploration stake on $2000 = $10. $495 + $10 = $505 > $500 → reject.
        conn.execute(
            "INSERT INTO poly_paper_bets(source, market_slug, token_id, side, "
            "entry_price, size_shares, cost_usd, placed_at) "
            "VALUES ('whale_copy', 'm0', 't0', 'YES', 0.4, 100, 495, 1)"
        )
        conn.commit()
        sig_id = _insert_signal(
            conn, wallet="0xnew", asset_id="t_new",
            signal_type="new_position", current_price=0.40,
            market_slug="m_new",
        )
        assert not place_copy_bet(
            conn, _row(conn, sig_id),
            bankroll_usd=2000.0, stake_pct=0.02,
        )
        n = conn.execute(
            "SELECT COUNT(*) FROM poly_paper_bets WHERE source = 'whale_copy'"
        ).fetchone()[0]
        assert n == 1
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
