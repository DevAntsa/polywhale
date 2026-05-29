"""Tests for maker-side routing shadow observer."""

from pathlib import Path

from polywhale.db import connect, run_migrations
from polywhale.maker_router import (
    DEFAULT_MAKER_WAIT_S,
    ROUTE_MAKER,
    ROUTE_MAKER_FALLBACK,
    ROUTE_TAKER,
    RouteDecision,
    decide_entry_route,
    routing_report,
    simulate_buy_yes_fill,
    simulate_sell_yes_fill,
)
from polywhale.whale_sizing import fee_for_category, maker_rebate_for_category


def _insert_book_snapshot(
    conn,
    *,
    token_id: str,
    best_bid: float,
    best_ask: float,
    captured_at: int,
    market_slug: str = "nba-okc-sas-2026-05-28",
) -> None:
    conn.execute(
        "INSERT INTO polymarket_books(market_slug, token_id, outcome, "
        "captured_at, server_ts, best_bid, best_ask, spread, book_json) "
        "VALUES (?, ?, 'Yes', ?, ?, ?, ?, ?, '{}')",
        (
            market_slug, token_id, captured_at, captured_at,
            best_bid, best_ask, best_ask - best_bid,
        ),
    )
    conn.commit()


def _signal_row(**fields):
    """Minimal stand-in for a sqlite3.Row — supports __getitem__."""
    defaults = {
        "old_size": None,
        "new_size": 100_000,
        "latest_captured_at": 1000,
        "detected_at": 1000,
        "asset_id": "t1",
    }
    defaults.update(fields)
    class _Row(dict):
        def __getitem__(self, key):
            return super().__getitem__(key)
    return _Row(defaults)


def test_decide_entry_route_default_is_maker() -> None:
    sig = _signal_row(old_size=None, new_size=100_000)
    d = decide_entry_route(sig, 0.50)
    assert d.route == ROUTE_MAKER
    assert d.max_wait_s == DEFAULT_MAKER_WAIT_S
    assert d.limit_price == 0.50


def test_decide_entry_route_time_critical_size_jump_is_taker() -> None:
    # 100% size jump → time critical
    sig = _signal_row(old_size=100_000, new_size=300_000)
    d = decide_entry_route(sig, 0.50)
    assert d.route == ROUTE_TAKER


def test_simulate_buy_yes_fill_maker_fills_when_ask_drops(tmp_path: Path) -> None:
    """A maker bid at 0.50 should fill if best_ask later drops to <= 0.50."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        # Add a snapshot 60s after signal where ask drops to 0.50
        _insert_book_snapshot(
            conn, token_id="t1", best_bid=0.49, best_ask=0.50,
            captured_at=1060,
        )
        decision = RouteDecision(
            route=ROUTE_MAKER, limit_price=0.50,
            max_wait_s=DEFAULT_MAKER_WAIT_S, reason="test",
        )
        result = simulate_buy_yes_fill(
            conn, token_id="t1", stake_usd=30.0, signal_ts=1000,
            decision=decision, category="sports",
        )
        assert result.route == ROUTE_MAKER
        assert result.fill_price == 0.50
        # Sports fee 0.0075, rebate 0.25 → captured = 30 * 0.0075 * 0.25 = $0.05625
        expected_rebate = 30.0 * fee_for_category("sports") * maker_rebate_for_category("sports")
        assert abs(result.fee_usd - expected_rebate) < 1e-6
        assert result.fee_usd > 0  # positive = rebate captured
    finally:
        conn.close()


def test_simulate_buy_yes_fill_maker_falls_back_when_no_touch(tmp_path: Path) -> None:
    """If ask never drops to limit, route → fallback taker at final ask."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        # Asks stay above 0.50 the whole window
        _insert_book_snapshot(
            conn, token_id="t1", best_bid=0.52, best_ask=0.53, captured_at=1060,
        )
        _insert_book_snapshot(
            conn, token_id="t1", best_bid=0.52, best_ask=0.54, captured_at=1240,
        )
        decision = RouteDecision(
            route=ROUTE_MAKER, limit_price=0.50,
            max_wait_s=DEFAULT_MAKER_WAIT_S, reason="test",
        )
        result = simulate_buy_yes_fill(
            conn, token_id="t1", stake_usd=30.0, signal_ts=1000,
            decision=decision, category="sports",
        )
        assert result.route == ROUTE_MAKER_FALLBACK
        assert result.fill_price == 0.54  # last best_ask in window
        assert result.fee_usd == -30.0 * fee_for_category("sports")
        assert result.fee_usd < 0  # taker fee paid
    finally:
        conn.close()


def test_simulate_buy_yes_fill_taker_immediate(tmp_path: Path) -> None:
    """Taker route returns immediately with the limit price as fill and a fee paid."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        decision = RouteDecision(
            route=ROUTE_TAKER, limit_price=0.50,
            max_wait_s=0, reason="time critical",
        )
        result = simulate_buy_yes_fill(
            conn, token_id="t1", stake_usd=30.0, signal_ts=1000,
            decision=decision, category="sports",
        )
        assert result.route == ROUTE_TAKER
        assert result.fill_price == 0.50
        assert result.fee_usd == -30.0 * fee_for_category("sports")
    finally:
        conn.close()


def test_simulate_buy_yes_fill_geopolitics_is_free(tmp_path: Path) -> None:
    """Geopolitics category has 0 fees so even taker pays nothing."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        decision = RouteDecision(
            route=ROUTE_TAKER, limit_price=0.50,
            max_wait_s=0, reason="test",
        )
        result = simulate_buy_yes_fill(
            conn, token_id="t1", stake_usd=100.0, signal_ts=1000,
            decision=decision, category="geopolitics",
        )
        assert result.fee_usd == 0.0
    finally:
        conn.close()


def test_simulate_sell_yes_fill_maker_fills_when_bid_rises(tmp_path: Path) -> None:
    """A maker ask at 0.55 should fill when best_bid rises to >= 0.55."""
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        _insert_book_snapshot(
            conn, token_id="t1", best_bid=0.55, best_ask=0.56, captured_at=1060,
        )
        decision = RouteDecision(
            route=ROUTE_MAKER, limit_price=0.55,
            max_wait_s=DEFAULT_MAKER_WAIT_S, reason="test",
        )
        result = simulate_sell_yes_fill(
            conn, token_id="t1", proceeds_usd=55.0, signal_ts=1000,
            decision=decision, category="sports",
        )
        assert result.route == ROUTE_MAKER
        assert result.fill_price == 0.55
        assert result.fee_usd > 0  # rebate captured
    finally:
        conn.close()


def test_routing_report_aggregates_route_counts(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    try:
        run_migrations(conn)
        # Insert three bets with different entry routes
        for route, fee in [
            (ROUTE_MAKER, +0.05), (ROUTE_TAKER, -0.20), (ROUTE_MAKER_FALLBACK, -0.20),
        ]:
            conn.execute(
                "INSERT INTO poly_paper_bets(source, market_slug, token_id, "
                "side, entry_price, size_shares, cost_usd, placed_at, "
                "entry_route, entry_fee_usd) "
                "VALUES ('whale_copy', 'm1', 't1', 'YES', 0.5, 60, 30, 1, ?, ?)",
                (route, fee),
            )
        conn.commit()
        r = routing_report(conn)
        assert r["entry_maker"] == 1
        assert r["entry_taker"] == 1
        assert r["entry_fallback"] == 1
        assert r["entry_n"] == 3
        assert abs(r["entry_fee_total"] - (0.05 - 0.20 - 0.20)) < 1e-9
    finally:
        conn.close()
