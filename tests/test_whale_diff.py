from pathlib import Path

from polywhale.db import connect, run_migrations
from polywhale.whale_diff import (
    SIG_ADDED,
    SIG_CLOSED,
    SIG_NEW,
    SIG_REDUCED,
    detect_signals_for_wallet,
    persist_signals,
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
