"""Tests for the WebSocket trade-event → whale-snapshot dispatcher."""

import asyncio
from pathlib import Path

import pytest

from polywhale.db import connect, run_migrations
from polywhale.polymarket import WhalePosition
from polywhale.whale_refresh import upsert_manual
from polywhale.ws_dispatcher import WALLET_THROTTLE_S, WhaleSnapshotDispatcher


class _StubClient:
    def __init__(self, positions: list[WhalePosition]) -> None:
        self._positions = positions
        self.calls = 0

    def get_whale_positions(self, wallet, *, size_threshold=10.0):
        self.calls += 1
        return [p for p in self._positions if p.wallet == wallet.lower()]


def _pos(wallet: str, asset_id: str, size: float = 100.0) -> WhalePosition:
    return WhalePosition(
        wallet=wallet,
        asset_id=asset_id,
        condition_id="0xcond",
        market_slug="m",
        event_slug="e",
        title="Test",
        outcome="Yes",
        size=size,
        avg_price=0.5,
        current_price=0.55,
        current_value=size * 0.55,
        initial_value=size * 0.5,
        cash_pnl=size * 0.05,
        realized_pnl=0,
        percent_pnl=10.0,
        end_date="2026-12-31",
        neg_risk=False,
    )


def _seed_whale_with_position(conn, wallet: str, asset_id: str) -> None:
    upsert_manual(conn, wallet=wallet)
    conn.execute(
        "INSERT INTO whale_positions(wallet, asset_id, size, captured_at) "
        "VALUES (?, ?, ?, ?)",
        (wallet, asset_id, 100.0, 1000),
    )
    conn.commit()


def test_dispatcher_resolves_wallets_holding_asset(tmp_path: Path) -> None:
    """on_trade should find the wallets that hold the traded asset."""
    conn = connect(tmp_path / "t.sqlite", check_same_thread=False)
    try:
        run_migrations(conn)
        _seed_whale_with_position(conn, "0xa", "tok-shared")
        _seed_whale_with_position(conn, "0xb", "tok-shared")
        _seed_whale_with_position(conn, "0xc", "tok-other")
        client = _StubClient([_pos("0xa", "tok-shared")])
        d = WhaleSnapshotDispatcher(
            conn, client, place_copy_bets=False, send_alerts=False,
        )
        # No actual asyncio loop needed for resolution — just call the helper
        wallets = d._wallets_holding("tok-shared")
        assert set(wallets) == {"0xa", "0xb"}
        wallets = d._wallets_holding("tok-other")
        assert wallets == ["0xc"]
        wallets = d._wallets_holding("tok-nobody")
        assert wallets == []
    finally:
        conn.close()


def test_dispatcher_throttles_repeat_events(tmp_path: Path) -> None:
    """Two trade events for the same asset shouldn't double-snapshot the wallet."""
    conn = connect(tmp_path / "t.sqlite", check_same_thread=False)
    try:
        run_migrations(conn)
        _seed_whale_with_position(conn, "0xa", "tok1")
        client = _StubClient([_pos("0xa", "tok1")])
        d = WhaleSnapshotDispatcher(
            conn, client, place_copy_bets=False, send_alerts=False,
        )

        async def driver():
            await d.on_trade({"asset_id": "tok1", "event_type": "last_trade_price"})
            await d.on_trade({"asset_id": "tok1", "event_type": "last_trade_price"})
            # Let the flush task run.
            await asyncio.sleep(0.4)

        asyncio.run(driver())
        # The wallet should be in the throttle cache after the first flush.
        assert "0xa" in d._last_snapshot_at
        # Two events fired but only one fetch happened because throttle kicked
        # in before the second event was processed.
        assert client.calls == 1
    finally:
        conn.close()


def test_dispatcher_ignores_events_for_unwatched_assets(tmp_path: Path) -> None:
    """A trade on an asset no whale holds should not trigger a fetch."""
    conn = connect(tmp_path / "t.sqlite", check_same_thread=False)
    try:
        run_migrations(conn)
        _seed_whale_with_position(conn, "0xa", "tok1")
        client = _StubClient([_pos("0xa", "tok1")])
        d = WhaleSnapshotDispatcher(
            conn, client, place_copy_bets=False, send_alerts=False,
        )

        async def driver():
            await d.on_trade({"asset_id": "tok-not-watched"})
            await asyncio.sleep(0.4)

        asyncio.run(driver())
        assert client.calls == 0
    finally:
        conn.close()


def test_dispatcher_skips_events_with_no_asset_id(tmp_path: Path) -> None:
    """A malformed event without asset_id should be silently ignored."""
    conn = connect(tmp_path / "t.sqlite", check_same_thread=False)
    try:
        run_migrations(conn)
        _seed_whale_with_position(conn, "0xa", "tok1")
        client = _StubClient([_pos("0xa", "tok1")])
        d = WhaleSnapshotDispatcher(
            conn, client, place_copy_bets=False, send_alerts=False,
        )

        async def driver():
            await d.on_trade({"event_type": "last_trade_price"})  # no asset_id
            await asyncio.sleep(0.4)

        asyncio.run(driver())
        assert client.calls == 0
    finally:
        conn.close()


@pytest.mark.parametrize("evt_key", ["asset_id", "token_id"])
def test_dispatcher_accepts_either_asset_key(tmp_path: Path, evt_key: str) -> None:
    """Polymarket WS uses different keys on different events; accept both."""
    conn = connect(tmp_path / "t.sqlite", check_same_thread=False)
    try:
        run_migrations(conn)
        _seed_whale_with_position(conn, "0xa", "tok1")
        client = _StubClient([_pos("0xa", "tok1")])
        d = WhaleSnapshotDispatcher(
            conn, client, place_copy_bets=False, send_alerts=False,
        )

        async def driver():
            await d.on_trade({evt_key: "tok1"})
            await asyncio.sleep(0.4)

        asyncio.run(driver())
        assert client.calls == 1
    finally:
        conn.close()


def test_throttle_constant_is_positive() -> None:
    """Sanity check on the throttle window."""
    assert WALLET_THROTTLE_S > 0
