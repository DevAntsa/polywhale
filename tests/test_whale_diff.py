from pathlib import Path

from polywhale.db import connect, run_migrations
from polywhale.whale_diff import (
    OVERREACTION_FLOOR,
    SIG_ADDED,
    SIG_CLOSED,
    SIG_NEW,
    SIG_REDUCED,
    conviction_discount_from_move,
    detect_signals_for_wallet,
    persist_signals,
    recent_price_move,
)


def _insert_position(
    conn,
    *,
    wallet: str,
    asset_id: str,
    size: float,
    captured_at: int,
    title: str = "T",
    outcome: str = "Yes",
):
    conn.execute(
        "INSERT INTO whale_positions(wallet, asset_id, market_slug, title, outcome, "
        "size, current_price, captured_at) "
        "VALUES (?, ?, 'mslug', ?, ?, ?, 0.5, ?)",
        (wallet, asset_id, title, outcome, size, captured_at),
    )
    conn.commit()


def test_new_position_signal(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        wallet = "0xw"
        _insert_position(
            conn, wallet=wallet, asset_id="A", size=1000, captured_at=100, title="Match A"
        )
        _insert_position(
            conn, wallet=wallet, asset_id="A", size=1000, captured_at=200, title="Match A"
        )
        _insert_position(
            conn, wallet=wallet, asset_id="B", size=500, captured_at=200, title="Match B"
        )
        signals = detect_signals_for_wallet(conn, wallet)
        types = [s.signal_type for s in signals]
        assert SIG_NEW in types
        new_b = next(s for s in signals if s.signal_type == SIG_NEW)
        assert new_b.asset_id == "B"
        assert new_b.new_size == 500
    finally:
        conn.close()


def test_added_size_signal(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        wallet = "0xw"
        _insert_position(conn, wallet=wallet, asset_id="A", size=1000, captured_at=100)
        _insert_position(conn, wallet=wallet, asset_id="A", size=2000, captured_at=200)
        signals = detect_signals_for_wallet(conn, wallet)
        added = [s for s in signals if s.signal_type == SIG_ADDED]
        assert len(added) == 1
        assert added[0].old_size == 1000
        assert added[0].new_size == 2000
    finally:
        conn.close()


def test_closed_position_signal(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        wallet = "0xw"
        _insert_position(conn, wallet=wallet, asset_id="A", size=1000, captured_at=100)
        _insert_position(conn, wallet=wallet, asset_id="A", size=1000, captured_at=200)
        # A small dummy position at 200 so the wallet has rows at 200 too
        _insert_position(conn, wallet=wallet, asset_id="B", size=500, captured_at=100)
        # At 200, B is gone
        signals = detect_signals_for_wallet(conn, wallet)
        closed = [s for s in signals if s.signal_type == SIG_CLOSED]
        assert len(closed) == 1
        assert closed[0].asset_id == "B"
        assert closed[0].new_size == 0.0
    finally:
        conn.close()


def test_reduced_size_signal(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        wallet = "0xw"
        _insert_position(conn, wallet=wallet, asset_id="A", size=2000, captured_at=100)
        _insert_position(conn, wallet=wallet, asset_id="A", size=500, captured_at=200)  # -75%
        signals = detect_signals_for_wallet(conn, wallet)
        reduced = [s for s in signals if s.signal_type == SIG_REDUCED]
        assert len(reduced) == 1
        assert reduced[0].old_size == 2000
        assert reduced[0].new_size == 500
    finally:
        conn.close()


def test_no_signal_if_only_one_snapshot(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _insert_position(conn, wallet="0xw", asset_id="A", size=1000, captured_at=100)
        assert detect_signals_for_wallet(conn, "0xw") == []
    finally:
        conn.close()


def test_min_size_filters_dust(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        wallet = "0xw"
        _insert_position(conn, wallet=wallet, asset_id="A", size=1000, captured_at=100)
        # New position at 200 but tiny
        _insert_position(conn, wallet=wallet, asset_id="A", size=1000, captured_at=200)
        _insert_position(conn, wallet=wallet, asset_id="B", size=50, captured_at=200)
        signals = detect_signals_for_wallet(conn, wallet, min_size=100)
        assert not any(s.signal_type == SIG_NEW for s in signals)
    finally:
        conn.close()


def _insert_book(
    conn,
    *,
    token_id: str,
    best_ask: float,
    captured_at: int,
) -> None:
    conn.execute(
        "INSERT INTO polymarket_books(market_slug, token_id, captured_at, best_ask, book_json) "
        "VALUES ('m', ?, ?, ?, '{}')",
        (token_id, captured_at, best_ask),
    )
    conn.commit()


def test_conviction_discount_no_move() -> None:
    # No price history => no penalty
    assert conviction_discount_from_move(None, current_price=0.5) == 1.0


def test_conviction_discount_small_move() -> None:
    # 2pp move is below trigger threshold (5pp) => no penalty
    assert conviction_discount_from_move(0.02, current_price=0.5) == 1.0


def test_conviction_discount_large_move_caps_at_floor() -> None:
    # 10pp move maxes out the penalty at OVERREACTION_FLOOR
    assert conviction_discount_from_move(0.10, current_price=0.5) == OVERREACTION_FLOOR
    # And the cap holds for even larger moves
    assert conviction_discount_from_move(0.30, current_price=0.5) == OVERREACTION_FLOOR


def test_recent_price_move_reads_book_history(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _insert_book(conn, token_id="A", best_ask=0.30, captured_at=100)
        _insert_book(conn, token_id="A", best_ask=0.42, captured_at=200)
        move = recent_price_move(conn, "A", since_ts=50)
        assert move is not None
        assert abs(move - 0.12) < 1e-9
    finally:
        conn.close()


def test_overreaction_filter_applies_on_new_signal(tmp_path: Path) -> None:
    """A whale enters a market that already moved 10pp toward YES -> conviction is discounted."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        # Book history: price went from 0.30 -> 0.42 (12pp up) over last 24h
        latest_ts = 1_000_000
        _insert_book(conn, token_id="A", best_ask=0.30, captured_at=latest_ts - 80_000)
        _insert_book(conn, token_id="A", best_ask=0.42, captured_at=latest_ts - 100)
        # Whale opens fresh position at latest snapshot
        _insert_position(
            conn, wallet="0xw", asset_id="A", size=1000, captured_at=latest_ts - 500_000
        )
        # Second snapshot has whale entering A (NEW)
        # First snapshot has no asset A to mark NEW. We make B at prev so we have 2 timestamps.
        conn.execute("DELETE FROM whale_positions WHERE asset_id = 'A'")
        conn.commit()
        _insert_position(
            conn, wallet="0xw", asset_id="B", size=1000, captured_at=latest_ts - 500_000
        )
        _insert_position(conn, wallet="0xw", asset_id="A", size=1000, captured_at=latest_ts)
        _insert_position(conn, wallet="0xw", asset_id="B", size=1000, captured_at=latest_ts)
        signals = detect_signals_for_wallet(conn, "0xw")
        new_a = next(s for s in signals if s.signal_type == SIG_NEW and s.asset_id == "A")
        assert new_a.recent_move_pct is not None
        assert abs(new_a.recent_move_pct - 0.12) < 1e-6
        assert new_a.conviction_discount is not None
        assert new_a.conviction_discount <= OVERREACTION_FLOOR + 1e-9
    finally:
        conn.close()


def test_persist_signals_dedupes(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        wallet = "0xw"
        _insert_position(conn, wallet=wallet, asset_id="A", size=1000, captured_at=100)
        _insert_position(conn, wallet=wallet, asset_id="A", size=1000, captured_at=200)
        _insert_position(conn, wallet=wallet, asset_id="B", size=500, captured_at=200)
        signals = detect_signals_for_wallet(conn, wallet)
        n1 = persist_signals(conn, signals)
        assert n1 > 0
        # Re-detect, persist again - should dedupe to 0 new rows
        n2 = persist_signals(conn, signals)
        assert n2 == 0
    finally:
        conn.close()
