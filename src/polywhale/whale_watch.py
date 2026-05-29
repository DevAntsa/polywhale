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
from concurrent.futures import ThreadPoolExecutor, as_completed

from polywhale.polymarket import PolymarketClient, WhalePosition

logger = logging.getLogger(__name__)

# Cap concurrent fetches to stay polite to data-api and keep memory bounded
# on the Hetzner CAX11 box.
PARALLEL_FETCH_MAX_WORKERS = 5


def snapshot_wallet(
    conn: sqlite3.Connection,
    client: PolymarketClient,
    wallet: str,
    *,
    size_threshold: float = 10.0,
) -> int:
    """Pull a wallet's open positions and persist them IF they differ from the latest stored
    snapshot. Returns count stored (0 means unchanged or empty).

    Change-only semantics keep disk growth bounded at 60s polling cadence.
    Limitation: a whale going from having positions to zero positions is not recorded;
    we'll miss the corresponding 'closed_position' signal in that rare case.
    """
    # data-api can return mixed-case proxyWallet; normalize so downstream
    # joins against whale_watchlist.wallet (always lowercase) match correctly.
    wallet = wallet.lower()
    positions = client.get_whale_positions(wallet, size_threshold=size_threshold)
    if not positions:
        logger.debug("snapshot_wallet(%s): no positions above threshold", wallet)
        return 0
    new_set = {(p.asset_id, round(float(p.size or 0), 6)) for p in positions}
    if not _positions_changed(conn, wallet, new_set):
        return 0
    now = int(time.time())
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
    logger.info("snapshot_wallet(%s): stored %d positions (changed)", wallet, len(rows))
    return len(rows)


def _positions_changed(
    conn: sqlite3.Connection,
    wallet: str,
    new_set: set[tuple[str, float]],
) -> bool:
    row = conn.execute(
        "SELECT MAX(captured_at) FROM whale_positions WHERE wallet = ?",
        (wallet,),
    ).fetchone()
    if not row or row[0] is None:
        return True
    latest_ts = int(row[0])
    prev_rows = conn.execute(
        "SELECT asset_id, size FROM whale_positions WHERE wallet = ? AND captured_at = ?",
        (wallet, latest_ts),
    ).fetchall()
    prev_set = {(r["asset_id"], round(float(r["size"] or 0), 6)) for r in prev_rows}
    return prev_set != new_set


def snapshot_wallets_parallel(
    conn: sqlite3.Connection,
    client: PolymarketClient,
    wallets: Iterable[str],
    *,
    size_threshold: float = 10.0,
    max_workers: int = PARALLEL_FETCH_MAX_WORKERS,
) -> int:
    """Snapshot many wallets concurrently.

    HTTP fetches run in a thread pool (data-api is I/O bound, gains are large).
    The SQLite write phase is serial on the single connection (sqlite3 is not
    thread-safe across cursors on one connection). Returns total snapshots stored.
    """
    targets = [w.lower() for w in wallets]
    if not targets:
        return 0

    fetched: dict[str, list[WhalePosition]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_to_wallet = {
            ex.submit(client.get_whale_positions, w, size_threshold=size_threshold): w
            for w in targets
        }
        for future in as_completed(future_to_wallet):
            wallet = future_to_wallet[future]
            try:
                fetched[wallet] = future.result()
            except Exception as exc:
                logger.warning("parallel fetch failed for %s: %s", wallet[:14], exc)

    snap_count = 0
    now = int(time.time())
    for wallet, positions in fetched.items():
        if not positions:
            continue
        new_set = {(p.asset_id, round(float(p.size or 0), 6)) for p in positions}
        if not _positions_changed(conn, wallet, new_set):
            continue
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
        snap_count += len(rows)
        logger.info(
            "snapshot_wallet(%s): stored %d positions (changed)", wallet, len(rows),
        )
    conn.commit()
    return snap_count


def prune_old_snapshots(conn: sqlite3.Connection, *, days: int = 30) -> dict:
    """Delete whale_positions and polymarket_books rows older than `days`.

    Run daily to bound disk usage at high polling cadence.
    """
    cutoff = int(time.time()) - days * 24 * 60 * 60
    wp = conn.execute(
        "DELETE FROM whale_positions WHERE captured_at < ?", (cutoff,)
    ).rowcount
    pb = conn.execute(
        "DELETE FROM polymarket_books WHERE captured_at < ?", (cutoff,)
    ).rowcount
    conn.commit()
    logger.info("prune_old_snapshots(days=%d): deleted %d positions, %d books", days, wp, pb)
    return {"whale_positions": int(wp or 0), "polymarket_books": int(pb or 0), "days": days}


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
        p.wallet.lower(),
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
