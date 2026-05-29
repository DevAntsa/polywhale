"""Polymarket CLOB WebSocket client.

Public market channel: trade and book-update events for subscribed asset_ids.
The wire format does NOT expose the maker wallet (Cycle 4 research confirmed)
so we can't tell from the event alone whose trade fired. We use the event as
a *trigger* — when ANY trade hits a market one of our whales holds, we
immediately fetch updated positions for the whales tracking that asset and
let whale_diff decide whether a signal landed.

The persistent timer (whale-fast.timer at 15s) remains as a safety net for
the cases where:
  - WebSocket reconnects are mid-flight
  - A whale opens a position in a market we weren't subscribed to (just got
    funded via a transfer, etc.)

Run as a daemon: `polywhale whale-ws --default`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable

import websockets

logger = logging.getLogger(__name__)

CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# How long to wait before reconnecting after a disconnect.
RECONNECT_BACKOFF_S = 5
# Ping/pong cadence — Polymarket times out idle connections.
PING_INTERVAL_S = 25
PING_TIMEOUT_S = 15
# Max asset_ids per subscribe call. Polymarket accepts many but be polite.
SUBSCRIBE_BATCH = 200


async def _send_subscribe(
    ws: websockets.WebSocketClientProtocol,
    asset_ids: list[str],
) -> None:
    """Send a market-channel subscribe frame for a batch of asset_ids."""
    if not asset_ids:
        return
    msg = {"type": "market", "assets_ids": asset_ids}
    await ws.send(json.dumps(msg))
    logger.info("ws subscribed to %d assets", len(asset_ids))


def _asset_ids_from_whale_positions(
    conn: sqlite3.Connection,
    *,
    lookback_hours: int = 24,
) -> list[str]:
    """Return distinct asset_ids touched by our active whales recently.

    We subscribe to anything seen in the last day so reopens-after-flat are
    still caught quickly. Older positions drop off automatically as we recycle.
    """
    cutoff_clause = (
        "AND captured_at > strftime('%s','now') - ?"
        if lookback_hours > 0
        else ""
    )
    sql = f"""
    SELECT DISTINCT wp.asset_id
    FROM whale_positions wp
    JOIN whale_watchlist ww ON ww.wallet = wp.wallet
    WHERE ww.active = 1 {cutoff_clause}
    """
    params = (lookback_hours * 3600,) if lookback_hours > 0 else ()
    return [r["asset_id"] for r in conn.execute(sql, params) if r["asset_id"]]


async def listen_market_events(
    conn: sqlite3.Connection,
    on_trade: Callable[[dict], Awaitable[None]],
    *,
    refresh_subs_every_s: int = 300,
    ws_url: str = CLOB_WS_URL,
) -> None:
    """Main consumer loop: connect, subscribe, dispatch trade events.

    Reconnects with backoff on any failure. Periodically re-derives the
    asset_id subscription set from current whale positions so newly opened
    markets are caught.
    """
    while True:
        try:
            async with websockets.connect(
                ws_url,
                ping_interval=PING_INTERVAL_S,
                ping_timeout=PING_TIMEOUT_S,
                max_size=2**24,  # 16 MB — Polymarket can emit big book frames
            ) as ws:
                asset_ids = _asset_ids_from_whale_positions(conn)
                logger.info(
                    "ws connected — subscribing to %d initial assets", len(asset_ids),
                )
                for i in range(0, len(asset_ids), SUBSCRIBE_BATCH):
                    await _send_subscribe(ws, asset_ids[i : i + SUBSCRIBE_BATCH])

                last_refresh = asyncio.get_event_loop().time()
                while True:
                    # Wake either when a message arrives or to refresh subs.
                    remaining = refresh_subs_every_s - (
                        asyncio.get_event_loop().time() - last_refresh
                    )
                    if remaining <= 0:
                        new_ids = set(_asset_ids_from_whale_positions(conn))
                        added = new_ids - set(asset_ids)
                        if added:
                            logger.info(
                                "ws refresh: %d new assets to subscribe", len(added),
                            )
                            for i in range(0, len(added), SUBSCRIBE_BATCH):
                                await _send_subscribe(
                                    ws, list(added)[i : i + SUBSCRIBE_BATCH],
                                )
                        asset_ids = list(new_ids)
                        last_refresh = asyncio.get_event_loop().time()
                        continue

                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    except TimeoutError:
                        continue
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.debug("ws non-json frame ignored")
                        continue
                    await _dispatch_message(msg, on_trade)
        except websockets.ConnectionClosed as exc:
            logger.warning("ws closed (%s) — reconnecting in %ds", exc, RECONNECT_BACKOFF_S)
        except Exception as exc:
            logger.warning("ws error (%s) — reconnecting in %ds", exc, RECONNECT_BACKOFF_S)
        await asyncio.sleep(RECONNECT_BACKOFF_S)


async def _dispatch_message(
    msg: dict | list,
    on_trade: Callable[[dict], Awaitable[None]],
) -> None:
    """Polymarket sends single events as dicts or batched as lists."""
    events = msg if isinstance(msg, list) else [msg]
    for evt in events:
        if not isinstance(evt, dict):
            continue
        event_type = evt.get("event_type") or evt.get("type")
        # Two event types we care about: a trade hit, or a book update where
        # last_trade_price changed (Polymarket emits both).
        if event_type in ("last_trade_price", "book"):
            with contextlib.suppress(Exception):
                await on_trade(evt)
