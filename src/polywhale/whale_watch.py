"""Whale-position snapshot loop.

Polls Polymarket's public data API for the open positions of watched wallets
and stores history. Used downstream as alpha-signal overlay - when our detection
says "Polymarket is mispriced on YES" *and* a sharp wallet is also on YES, the
opportunity is higher conviction.
"""

import json
import logging
import sqlite3
import time
from collections.abc import Iterable

from polywhale.polymarket import PolymarketClient, WhalePosition

logger = logging.getLogger(__name__)


def snapshot_wallet(
    conn: sqlite3.Connection,
    client: PolymarketClient,
    wallet: str,
    *,
    size_threshold: float = 10.0,
) -> int:
    """Pull a wallet's open positions and persist them. Returns count stored."""
    positions = client.get_whale_positions(wallet, size_threshold=size_threshold)
    now = int(time.time())
    if not positions:
        logger.info("snapshot_wallet(%s): no positions above threshold", wallet)
        return 0
    rows = [_to_row(p, now) for p in positions]
    conn.executemany(
        """
        INSERT INTO whale_positions(
            wallet, asset_id, condition_id, market_slug, event_slug, title,
            outcome, size, avg_price, current_price, current_value,
            initial_value, cash_pnl, realized_pnl, percent_pnl, end_date,
            neg_risk, captured_at, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    logger.info("snapshot_wallet(%s): stored %d positions", wallet, len(rows))
    return len(rows)


def watch_wallets(
    conn: sqlite3.Connection,
    client: PolymarketClient,
    wallets: Iterable[str],
    *,
    interval_s: int = 300,
    max_iterations: int | None = None,
    size_threshold: float = 10.0,
) -> int:
    """Poll each wallet every `interval_s` seconds. Returns total positions stored."""
    wallets = list(wallets)
    if not wallets:
        logger.warning("watch_wallets called with no wallets")
        return 0
    total = 0
    iteration = 0
    try:
        while max_iterations is None or iteration < max_iterations:
            iteration += 1
            for wallet in wallets:
                try:
                    total += snapshot_wallet(conn, client, wallet, size_threshold=size_threshold)
                except Exception as exc:
                    logger.warning("snapshot failed for %s: %s", wallet, exc)
            logger.info(
                "whale watch iteration %d done: total=%d positions stored",
                iteration,
                total,
            )
            if max_iterations is not None and iteration >= max_iterations:
                break
            time.sleep(interval_s)
    except KeyboardInterrupt:
        logger.info("watch_wallets interrupted after %d iteration(s)", iteration)
    return total


def _to_row(p: WhalePosition, now: int) -> tuple:
    return (
        p.wallet,
        p.asset_id,
        p.condition_id,
        p.market_slug,
        p.event_slug,
        p.title,
        p.outcome,
        p.size,
        p.avg_price,
        p.current_price,
        p.current_value,
        p.initial_value,
        p.cash_pnl,
        p.realized_pnl,
        p.percent_pnl,
        p.end_date,
        1 if p.neg_risk else 0,
        now,
        json.dumps(
            {
                "wallet": p.wallet,
                "asset_id": p.asset_id,
                "condition_id": p.condition_id,
                "market_slug": p.market_slug,
                "title": p.title,
                "outcome": p.outcome,
                "size": p.size,
                "avg_price": p.avg_price,
                "current_price": p.current_price,
                "cash_pnl": p.cash_pnl,
                "percent_pnl": p.percent_pnl,
            }
        ),
    )
