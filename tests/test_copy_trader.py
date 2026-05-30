import time
from pathlib import Path

from polywhale.copy_trader import (
    close_copy_bet,
    copy_equity_drawdown,
    copy_trade_stats,
    find_closed_copy_bet_by_exit_signal,
    find_open_copy_bet_for_signal,
    place_copy_bet,
    process_copy_trades,
)
from polywhale.db import connect, run_migrations
from polywhale.whale_sizing import EXPLORE_STAKE_PCT, MAX_PORTFOLIO_DEPLOY_PCT


def _insert_settled(conn, *, pnl: float, settled_at: int, cost: float = 50.0) -> None:
    """Insert a settled whale_copy bet directly (for drawdown-curve tests)."""
    conn.execute(
        "INSERT INTO poly_paper_bets(source, market_slug, token_id, side, "
        "entry_price, size_shares, cost_usd, placed_at, settled_at, pnl_usd) "
        "VALUES ('whale_copy', 'm', 't', 'YES', 0.5, 100, ?, 1, ?, ?)",
        (cost, settled_at, pnl),
    )
    conn.commit()

_BANKROLL = 2000.0
_EXPLORE = _BANKROLL * EXPLORE_STAKE_PCT  # default explore stake


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
        assert place_copy_bet(conn, sig_row, bankroll_usd=_BANKROLL, stake_pct=0.02)
        bet = find_open_copy_bet_for_signal(conn, sig_id)
        assert bet is not None
        # Exploration stake = EXPLORE_STAKE_PCT * bankroll
        assert abs(bet["cost_usd"] - _EXPLORE) < 0.5
        # Shares = stake / 0.40
        assert abs(bet["size_shares"] - _EXPLORE / 0.40) < 1.0
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
        place_copy_bet(conn, sig_row, bankroll_usd=_BANKROLL, stake_pct=0.02)
        bet = find_open_copy_bet_for_signal(conn, sig_id)
        # explore * 0.5 conviction
        assert abs(bet["cost_usd"] - _EXPLORE * 0.5) < 0.5
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
        place_copy_bet(conn, entry_row, bankroll_usd=_BANKROLL, stake_pct=0.02)
        # Whale exits at 0.55
        exit_sig = _insert_signal(conn, wallet="0xw", asset_id="t1",
                                  signal_type="closed_position", current_price=0.55,
                                  new_size=0, old_size=100_000)
        exit_row = _row(conn, exit_sig)
        pnl = close_copy_bet(conn, exit_row)
        assert pnl is not None
        # shares = explore/0.40; pnl = (0.55 - 0.40) * shares
        expected_pnl = (0.55 - 0.40) * (_EXPLORE / 0.40)
        assert abs(pnl - expected_pnl) < 0.1
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
        place_copy_bet(conn, _row(conn, new_sig), bankroll_usd=_BANKROLL, stake_pct=0.02)
        n_initial = conn.execute(
            "SELECT COUNT(*) FROM poly_paper_bets WHERE source = 'whale_copy'"
        ).fetchone()[0]
        assert n_initial == 1
        # Whale ADDS — should top up, not create a new row
        add_sig = _insert_signal(conn, wallet="0xw", asset_id="t1",
                                 signal_type="added_size", current_price=0.50,
                                 market_slug="m1", new_size=200_000, old_size=100_000)
        place_copy_bet(conn, _row(conn, add_sig), bankroll_usd=_BANKROLL, stake_pct=0.02)
        n_after = conn.execute(
            "SELECT COUNT(*) FROM poly_paper_bets WHERE source = 'whale_copy'"
        ).fetchone()[0]
        assert n_after == 1  # still one row
        bet = conn.execute(
            "SELECT * FROM poly_paper_bets WHERE source = 'whale_copy'"
        ).fetchone()
        assert bet["add_count"] == 1
        # cost = original explore + topup explore = 2 * explore
        assert abs(float(bet["cost_usd"]) - 2 * _EXPLORE) < 0.5
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
        # Open at 0.40 with explore stake
        new_sig = _insert_signal(conn, wallet="0xw", asset_id="t1",
                                 signal_type="new_position", current_price=0.40,
                                 market_slug="m1")
        place_copy_bet(conn, _row(conn, new_sig), bankroll_usd=_BANKROLL, stake_pct=0.02)
        # Add at 0.50 with explore stake
        add_sig = _insert_signal(conn, wallet="0xw", asset_id="t1",
                                 signal_type="added_size", current_price=0.50,
                                 market_slug="m1", new_size=200_000, old_size=100_000)
        place_copy_bet(conn, _row(conn, add_sig), bankroll_usd=_BANKROLL, stake_pct=0.02)
        bet = conn.execute(
            "SELECT entry_price, size_shares, cost_usd FROM poly_paper_bets "
            "WHERE source = 'whale_copy'"
        ).fetchone()
        expected_shares = _EXPLORE / 0.40 + _EXPLORE / 0.50
        expected_cost = 2 * _EXPLORE
        expected_vwap = expected_cost / expected_shares
        assert abs(float(bet["cost_usd"]) - expected_cost) < 0.5
        assert abs(float(bet["size_shares"]) - expected_shares) < 1.0
        assert abs(float(bet["entry_price"]) - expected_vwap) < 0.02
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
        result = process_copy_trades(conn, bankroll_usd=_BANKROLL, stake_pct=0.02)
        assert result["opened"] == 2
        assert result["closed"] == 0

        # Now process EXIT for one of them
        _insert_signal(conn, wallet="0xa", asset_id="ta", signal_type="closed_position",
                       current_price=0.40, new_size=0, old_size=100_000,
                       market_slug="market_a")
        result2 = process_copy_trades(conn, bankroll_usd=_BANKROLL, stake_pct=0.02)
        assert result2["opened"] == 0
        assert result2["closed"] == 1
        # shares = explore/0.30; pnl = (0.40 - 0.30) * shares
        expected_pnl = (0.40 - 0.30) * (_EXPLORE / 0.30)
        assert abs(result2["realized_pnl"] - expected_pnl) < 0.5
    finally:
        conn.close()


def test_place_copy_bet_refuses_when_portfolio_cap_full(tmp_path: Path) -> None:
    """New bets are skipped once MAX_PORTFOLIO_DEPLOY_PCT cap is engaged."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        cap_usd = _BANKROLL * MAX_PORTFOLIO_DEPLOY_PCT
        # Pre-populate just under the cap so the next explore stake pushes us over.
        prefilled = cap_usd - max(0.5, _EXPLORE / 2)
        conn.execute(
            "INSERT INTO poly_paper_bets(source, market_slug, token_id, side, "
            "entry_price, size_shares, cost_usd, placed_at) "
            "VALUES ('whale_copy', 'm0', 't0', 'YES', 0.4, 100, ?, 1)",
            (prefilled,),
        )
        conn.commit()
        sig_id = _insert_signal(
            conn, wallet="0xnew", asset_id="t_new",
            signal_type="new_position", current_price=0.40,
            market_slug="m_new",
        )
        assert not place_copy_bet(
            conn, _row(conn, sig_id),
            bankroll_usd=_BANKROLL, stake_pct=0.02,
        )
        n = conn.execute(
            "SELECT COUNT(*) FROM poly_paper_bets WHERE source = 'whale_copy'"
        ).fetchone()[0]
        assert n == 1
    finally:
        conn.close()


def test_place_copy_bet_skips_uncopyable_archetype(tmp_path: Path) -> None:
    """Cycle 2: wallets tagged with an uncopyable playbook archetype
    (news-arb, market-making, etc.) should have their entry signals dropped."""
    from polywhale.whale_refresh import set_archetype, upsert_manual
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        upsert_manual(conn, wallet="0xwa", label="newsbot")
        set_archetype(conn, "0xwa", "news-arb")
        sig_id = _insert_signal(
            conn, wallet="0xwa", asset_id="t1",
            signal_type="new_position", current_price=0.40,
        )
        assert place_copy_bet(
            conn, _row(conn, sig_id),
            bankroll_usd=_BANKROLL, stake_pct=0.02,
        ) is False
        n = conn.execute(
            "SELECT COUNT(*) FROM poly_paper_bets WHERE source = 'whale_copy'"
        ).fetchone()[0]
        assert n == 0
    finally:
        conn.close()


def test_place_copy_bet_allows_unclassified_wallets(tmp_path: Path) -> None:
    """A wallet with no archetype set should default to retail_copyable=1
    and trade normally."""
    from polywhale.whale_refresh import upsert_manual
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        upsert_manual(conn, wallet="0xnew", label=None)
        sig_id = _insert_signal(
            conn, wallet="0xnew", asset_id="t1",
            signal_type="new_position", current_price=0.40,
        )
        assert place_copy_bet(
            conn, _row(conn, sig_id),
            bankroll_usd=_BANKROLL, stake_pct=0.02,
        ) is True
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


# Max-drawdown kill switch (v1).

def test_copy_equity_drawdown_no_trades(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        dd, peak, cur = copy_equity_drawdown(conn, 2000.0)
        assert dd == 0.0
        assert peak == 2000.0
        assert cur == 2000.0
    finally:
        conn.close()


def test_copy_equity_drawdown_from_peak(tmp_path: Path) -> None:
    """Equity 2000 -> 2200 (peak) -> 2100 → drawdown ~4.5% off the peak."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _insert_settled(conn, pnl=200.0, settled_at=10)
        _insert_settled(conn, pnl=-100.0, settled_at=20)
        dd, peak, cur = copy_equity_drawdown(conn, 2000.0)
        assert abs(peak - 2200.0) < 1e-6
        assert abs(cur - 2100.0) < 1e-6
        assert abs(dd - (100.0 / 2200.0)) < 1e-6
    finally:
        conn.close()


def test_kill_switch_halts_new_entries_at_max_drawdown(tmp_path: Path) -> None:
    """A 50% realized drawdown halts new entries; nothing opens."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        # One -$1000 settled loss on a $2000 bankroll → 50% drawdown.
        _insert_settled(conn, pnl=-1000.0, settled_at=10)
        # A fresh entry signal that would normally open.
        _insert_signal(conn, wallet="0xnew", asset_id="a1",
                       signal_type="new_position", current_price=0.40)
        res = process_copy_trades(conn, bankroll_usd=2000.0, stake_pct=0.02)
        assert res["killed"] is True
        assert res["drawdown_pct"] >= 50.0
        assert res["opened"] == 0
    finally:
        conn.close()


def test_kill_switch_still_allows_exits(tmp_path: Path) -> None:
    """While killed, open positions can still be closed (exits not halted)."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        # Open a position BEFORE the drawdown trips.
        entry = _insert_signal(conn, wallet="0xw", asset_id="a1",
                               signal_type="new_position", current_price=0.40)
        assert place_copy_bet(conn, _row(conn, entry), bankroll_usd=2000.0, stake_pct=0.02)
        # Now force a 50% drawdown.
        _insert_settled(conn, pnl=-1000.0, settled_at=10)
        # An exit signal for that position.
        _insert_signal(conn, wallet="0xw", asset_id="a1",
                       signal_type="closed_position", current_price=0.55,
                       new_size=0, old_size=100_000)
        res = process_copy_trades(conn, bankroll_usd=2000.0, stake_pct=0.02)
        assert res["killed"] is True
        assert res["opened"] == 0      # new entry signal halted
        assert res["closed"] >= 1      # but the exit went through
    finally:
        conn.close()


def test_no_kill_when_drawdown_small(tmp_path: Path) -> None:
    """A shallow drawdown does not trip the switch; entries proceed."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _insert_settled(conn, pnl=-100.0, settled_at=10)  # 5% drawdown
        _insert_signal(conn, wallet="0xnew", asset_id="a1",
                       signal_type="new_position", current_price=0.40)
        res = process_copy_trades(conn, bankroll_usd=2000.0, stake_pct=0.02)
        assert res["killed"] is False
        assert res["opened"] == 1
    finally:
        conn.close()
