"""Polymarket depth-measurement loop.

Polls the CLOB /book endpoint for a set of (market_slug, token_id, outcome) tuples
on a fixed cadence; stores full snapshots to SQLite. Foundation for the empirical
"is Polymarket actually depth-viable for our scale" question.
"""

import json
import logging
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass

from polywhale.polymarket import PolyBook, PolymarketClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WatchTarget:
    market_slug: str
    token_id: str
    outcome: str | None


def take_snapshot(
    conn: sqlite3.Connection,
    client: PolymarketClient,
    target: WatchTarget,
) -> int:
    """Pull one book snapshot for `target`; persist to polymarket_books. Returns snapshot_id."""
    book = client.get_book(target.token_id)
    return _store(conn, target, book, captured_at=int(time.time()))


def watch_loop(
    conn: sqlite3.Connection,
    client: PolymarketClient,
    targets: Iterable[WatchTarget],
    *,
    interval_s: int = 60,
    max_iterations: int | None = None,
) -> int:
    """Poll all targets every `interval_s` seconds. Returns total snapshots taken.

    If `max_iterations` is None, loops until interrupted. Otherwise stops after N rounds.
    """
    targets = list(targets)
    if not targets:
        logger.warning("watch_loop called with no targets")
        return 0
    total = 0
    iteration = 0
    try:
        while max_iterations is None or iteration < max_iterations:
            iteration += 1
            for target in targets:
                try:
                    take_snapshot(conn, client, target)
                    total += 1
                except Exception as exc:
                    logger.warning("snapshot failed for %s: %s", target.market_slug, exc)
            logger.info(
                "watch iteration %d done: %d total snapshots, %d targets",
                iteration,
                total,
                len(targets),
            )
            if max_iterations is not None and iteration >= max_iterations:
                break
            time.sleep(interval_s)
    except KeyboardInterrupt:
        logger.info("watch_loop interrupted after %d iteration(s)", iteration)
    return total


def _store(
    conn: sqlite3.Connection,
    target: WatchTarget,
    book: PolyBook,
    *,
    captured_at: int,
) -> int:
    bid_depth_5 = book.depth_within(side="bid", pct=0.05)
    ask_depth_5 = book.depth_within(side="ask", pct=0.05)
    payload = {
        "market": book.market,
        "asset_id": book.asset_id,
        "server_ts": book.server_ts,
        "bids": [{"price": b.price, "size": b.size} for b in book.bids],
        "asks": [{"price": a.price, "size": a.size} for a in book.asks],
        "last_trade_price": book.last_trade_price,
        "tick_size": book.tick_size,
        "neg_risk": book.neg_risk,
    }
    cur = conn.execute(
        """
        INSERT INTO polymarket_books (
            market_slug, token_id, outcome, captured_at, server_ts,
            best_bid, best_ask, spread,
            bid_depth_top5pc, ask_depth_top5pc,
            last_trade_price, tick_size, neg_risk, book_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            target.market_slug,
            target.token_id,
            target.outcome,
            captured_at,
            book.server_ts,
            book.best_bid,
            book.best_ask,
            book.spread,
            bid_depth_5,
            ask_depth_5,
            book.last_trade_price,
            book.tick_size,
            1 if book.neg_risk else 0,
            json.dumps(payload),
        ),
    )
    conn.commit()
    snapshot_id = cur.lastrowid or 0
    logger.debug(
        "stored snapshot #%d slug=%s outcome=%s bid=%s ask=%s",
        snapshot_id,
        target.market_slug,
        target.outcome,
        book.best_bid,
        book.best_ask,
    )
    return snapshot_id
