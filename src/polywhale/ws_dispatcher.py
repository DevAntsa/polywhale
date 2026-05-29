"""WebSocket trade-event → whale-snapshot dispatcher.

When the CLOB WebSocket reports a trade on a market, we don't know who traded
(maker_address is not exposed). What we can do: immediately snapshot the
positions of any whale on our watchlist who currently holds the asset that
was traded. Whale_diff then decides if a signal landed.

This compresses detection latency from "next 15s timer fire" to "~1 second
after the trade hits the market channel" for markets we're tracking.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time

from polywhale.copy_trader import process_copy_trades
from polywhale.polymarket import PolymarketClient
from polywhale.whale_alerter import send_signal_alerts
from polywhale.whale_diff import detect_for_wallets, persist_signals
from polywhale.whale_watch import snapshot_wallets_parallel

logger = logging.getLogger(__name__)

# Throttle: never re-snapshot the same wallet more than once per this window,
# even if the WebSocket fires repeatedly for the same asset.
WALLET_THROTTLE_S = 5


class WhaleSnapshotDispatcher:
    """Coalesces trade events into batched whale snapshots."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        client: PolymarketClient,
        *,
        bankroll_usd: float = 2000.0,
        stake_pct: float = 0.02,
        send_alerts: bool = True,
        place_copy_bets: bool = True,
    ) -> None:
        self.conn = conn
        self.client = client
        self.bankroll_usd = bankroll_usd
        self.stake_pct = stake_pct
        self.send_alerts = send_alerts
        self.place_copy_bets = place_copy_bets
        self._last_snapshot_at: dict[str, float] = {}
        self._coalesce_buffer: set[str] = set()
        self._flush_task: asyncio.Task | None = None

    def _wallets_holding(self, asset_id: str) -> list[str]:
        """Active whales whose latest snapshot includes this asset_id."""
        rows = self.conn.execute(
            """
            SELECT DISTINCT wp.wallet
            FROM whale_positions wp
            JOIN whale_watchlist ww ON ww.wallet = wp.wallet
            WHERE ww.active = 1 AND wp.asset_id = ?
            """,
            (asset_id,),
        ).fetchall()
        return [r["wallet"] for r in rows]

    async def on_trade(self, evt: dict) -> None:
        """Trade event handler — wired into poly_ws.listen_market_events."""
        asset_id = evt.get("asset_id") or evt.get("token_id")
        if not asset_id:
            return
        wallets = self._wallets_holding(asset_id)
        if not wallets:
            return

        # Throttle: only queue wallets we haven't snapshotted recently.
        now = time.monotonic()
        fresh = [
            w for w in wallets
            if now - self._last_snapshot_at.get(w, 0) > WALLET_THROTTLE_S
        ]
        if not fresh:
            return

        self._coalesce_buffer.update(fresh)
        # If a flush isn't already scheduled, schedule one in 200ms so
        # bursts of trades across multiple events get batched.
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_after(0.2))

    async def _flush_after(self, delay_s: float) -> None:
        await asyncio.sleep(delay_s)
        wallets = list(self._coalesce_buffer)
        self._coalesce_buffer.clear()
        if not wallets:
            return
        try:
            # Run the synchronous snapshot+diff path in a thread so the WS
            # event loop isn't blocked by HTTP and SQLite.
            await asyncio.to_thread(self._snapshot_and_dispatch, wallets)
        except Exception as exc:
            logger.warning("ws dispatcher flush failed: %s", exc)

    def _snapshot_and_dispatch(self, wallets: list[str]) -> None:
        snap_count = snapshot_wallets_parallel(self.conn, self.client, wallets)
        for w in wallets:
            self._last_snapshot_at[w] = time.monotonic()
        if snap_count == 0:
            return
        signals = detect_for_wallets(self.conn, wallets)
        stored = persist_signals(self.conn, signals)
        logger.info(
            "ws dispatcher: snapshotted %d wallets, persisted %d signals",
            len(wallets), stored,
        )
        if stored > 0 and self.place_copy_bets:
            try:
                process_copy_trades(
                    self.conn,
                    bankroll_usd=self.bankroll_usd,
                    stake_pct=self.stake_pct,
                )
            except Exception as exc:
                logger.warning("ws dispatcher copy_trade failed: %s", exc)
        if stored > 0 and self.send_alerts:
            try:
                send_signal_alerts(self.conn)
            except Exception as exc:
                logger.warning("ws dispatcher alert send failed: %s", exc)
