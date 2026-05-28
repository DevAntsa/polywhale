"""Tests for friction measurement instrumentation."""

from pathlib import Path

from polywhale.db import connect, run_migrations
from polywhale.friction_observer import (
    compute_friction_report,
    get_book_at,
    snapshot_entry_friction,
    snapshot_exit_friction,
)


def _insert_book(
    conn, *, token_id: str, captured_at: int,
    best_bid: float | None = 0.42, best_ask: float | None = 0.45,
):
    conn.execute(
        "INSERT INTO polymarket_books(market_slug, token_id, captured_at, "
        "best_bid, best_ask, book_json) "
        "VALUES ('m', ?, ?, ?, ?, '{}')",
        (token_id, captured_at, best_bid, best_ask),
    )
    conn.commit()


def _insert_bet(conn, *, entry_price: float = 0.40, size_shares: float = 100.0):
    cur = conn.execute(
        "INSERT INTO poly_paper_bets(source, market_slug, token_id, side, "
        "entry_price, size_shares, cost_usd, placed_at) "
        "VALUES ('whale_copy', 'm', 'asset1', 'YES', ?, ?, 40, 1)",
        (entry_price, size_shares),
    )
    conn.commit()
    return cur.lastrowid


def test_get_book_at_returns_closest(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _insert_book(conn, token_id="a1", captured_at=1000, best_ask=0.45)
        _insert_book(conn, token_id="a1", captured_at=1100, best_ask=0.46)
        _insert_book(conn, token_id="a1", captured_at=1200, best_ask=0.47)
        r = get_book_at(conn, "a1", 1090)
        assert r is not None
        assert float(r["best_ask"]) == 0.46  # closest to 1090 is 1100
    finally:
        conn.close()


def test_get_book_at_returns_none_when_no_coverage(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        # No book inserted
        r = get_book_at(conn, "a1", 1000)
        assert r is None
    finally:
        conn.close()


def test_snapshot_entry_records_slippage(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        bet_id = _insert_bet(conn, entry_price=0.40)
        _insert_book(conn, token_id="asset1", captured_at=1000, best_ask=0.42)
        result = snapshot_entry_friction(
            conn, bet_id=bet_id, asset_id="asset1",
            signal_ts=1000, paper_entry_price=0.40,
        )
        assert result["recorded"] is True
        # slippage = (0.42 - 0.40) / 0.40 = 0.05 = 5%
        assert abs(result["slippage_pct"] - 0.05) < 1e-4
        row = conn.execute(
            "SELECT hypothetical_real_entry, entry_slippage_pct FROM poly_paper_bets "
            "WHERE bet_id = ?", (bet_id,)
        ).fetchone()
        assert abs(float(row["hypothetical_real_entry"]) - 0.42) < 1e-6
        assert abs(float(row["entry_slippage_pct"]) - 0.05) < 1e-4
    finally:
        conn.close()


def test_snapshot_entry_skips_no_book_coverage(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        bet_id = _insert_bet(conn)
        result = snapshot_entry_friction(
            conn, bet_id=bet_id, asset_id="no_book",
            signal_ts=1000, paper_entry_price=0.40,
        )
        assert result["recorded"] is False
        assert "no_book_coverage" in result["reason"]
    finally:
        conn.close()


def test_snapshot_exit_records_slippage(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        bet_id = _insert_bet(conn)
        # Paper exit at 0.55, real bid at 0.53 → we'd get $0.02 less
        _insert_book(conn, token_id="asset1", captured_at=2000, best_bid=0.53)
        result = snapshot_exit_friction(
            conn, bet_id=bet_id, asset_id="asset1",
            signal_ts=2000, paper_exit_price=0.55,
        )
        assert result["recorded"] is True
        # slippage = (0.55 - 0.53) / 0.55 ≈ 0.0364
        assert abs(result["slippage_pct"] - 0.0364) < 1e-3
    finally:
        conn.close()


def test_compute_friction_report_aggregates(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        # Create 3 closed bets with friction observations
        for i in range(3):
            bet_id = _insert_bet(conn, entry_price=0.40)
            _insert_book(
                conn, token_id=f"asset_{i}", captured_at=1000,
                best_ask=0.42 + i * 0.01, best_bid=0.50 - i * 0.005,
            )
            conn.execute(
                "INSERT INTO polymarket_books(market_slug, token_id, captured_at, "
                "best_bid, best_ask, book_json) VALUES ('m', ?, 2000, ?, ?, '{}')",
                (f"asset_{i}", 0.50 - i * 0.005, 0.52),
            )
            # Update the bet to use this asset
            conn.execute(
                "UPDATE poly_paper_bets SET token_id = ?, settled_at = 3000, "
                "payout_per_share = 0.55, pnl_usd = 15.0 WHERE bet_id = ?",
                (f"asset_{i}", bet_id),
            )
            snapshot_entry_friction(
                conn, bet_id=bet_id, asset_id=f"asset_{i}",
                signal_ts=1000, paper_entry_price=0.40,
            )
            snapshot_exit_friction(
                conn, bet_id=bet_id, asset_id=f"asset_{i}",
                signal_ts=2000, paper_exit_price=0.55,
            )
        conn.commit()
        rpt = compute_friction_report(conn)
        assert rpt["covered_bets"] == 3
        assert rpt["entry_slippage_observations"] == 3
        assert rpt["entry_slippage_mean_pct"] > 0  # ask > paper entry
    finally:
        conn.close()


def test_compute_friction_report_handles_no_observations(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        rpt = compute_friction_report(conn)
        assert rpt["covered_bets"] == 0
        assert "no friction observations" in rpt["message"]
    finally:
        conn.close()
