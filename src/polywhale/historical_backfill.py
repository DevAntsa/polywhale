"""Paginate `data-api/activity` per wallet and store raw events for backtest.

Polymarket's data-api/activity returns up to 500 events per call. We paginate
with offset until we hit either empty results or a max-depth cap. Each wallet's
events get stored in whale_activity_history (unique-keyed on
wallet+transaction_hash+type+outcome_index to handle re-fetches gracefully).

This is the data layer that historical_backtest.py + walk_forward.py sit on top of.

Resolution lookups (gamma) get cached in market_resolutions so reruns of
position reconstruction don't re-hit the API.
"""

import json
import logging
import sqlite3
import time

from polywhale.polymarket import PolymarketClient

logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 500
DEFAULT_MAX_OFFSET = 5000   # bail past this; API seems to error past ~5K
PAGE_SLEEP_S = 0.2          # rate-limit politeness


def backfill_wallet_activity(
    conn: sqlite3.Connection,
    client: PolymarketClient,
    wallet: str,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_offset: int = DEFAULT_MAX_OFFSET,
    sleep_s: float = PAGE_SLEEP_S,
) -> dict:
    """Pull historical activity for one wallet, paginate until exhausted.

    Returns: {'inserted': int, 'skipped_dup': int, 'pages': int, 'oldest_ts': int|None}
    """
    inserted = 0
    skipped = 0
    pages = 0
    oldest_ts: int | None = None
    now = int(time.time())
    offset = 0
    while offset <= max_offset:
        events = _get_activity_with_offset(client, wallet, offset, page_size)
        if not events:
            break
        pages += 1
        for ev in events:
            inserted_one = _insert_event(conn, wallet, ev, now)
            if inserted_one:
                inserted += 1
            else:
                skipped += 1
            ts = ev.get("timestamp")
            if ts is not None:
                ts_int = int(ts)
                if oldest_ts is None or ts_int < oldest_ts:
                    oldest_ts = ts_int
        conn.commit()
        if len(events) < page_size:
            # Last page
            break
        offset += page_size
        time.sleep(sleep_s)
    logger.info(
        "backfill wallet=%s pages=%d inserted=%d dup=%d oldest_ts=%s",
        wallet[:14], pages, inserted, skipped, oldest_ts,
    )
    return {"inserted": inserted, "skipped_dup": skipped, "pages": pages, "oldest_ts": oldest_ts}


def _get_activity_with_offset(
    client: PolymarketClient, wallet: str, offset: int, limit: int
) -> list[dict]:
    """data-api/activity supports offset via query param. PolymarketClient.get_activity
    doesn't expose it, so we use the underlying httpx client directly."""
    try:
        resp = client._data.get(
            "/activity",
            params={"user": wallet, "limit": str(limit), "offset": str(offset)},
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as exc:
        logger.warning(
            "activity offset fetch failed wallet=%s offset=%d: %s",
            wallet[:14], offset, exc,
        )
        return []


def _insert_event(conn: sqlite3.Connection, wallet: str, ev: dict, now: int) -> bool:
    """Insert one event, returning True if new (False = duplicate)."""
    try:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO whale_activity_history(
                wallet, timestamp, type, condition_id, asset, side, price, size,
                usdc_size, outcome, outcome_index, market_slug, title,
                transaction_hash, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                wallet.lower(),
                int(ev.get("timestamp") or 0),
                ev.get("type") or "",
                ev.get("conditionId"),
                ev.get("asset"),
                ev.get("side"),
                _maybe_float(ev.get("price")),
                _maybe_float(ev.get("size")),
                _maybe_float(ev.get("usdcSize")),
                ev.get("outcome"),
                _maybe_int(ev.get("outcomeIndex")),
                ev.get("slug"),
                ev.get("title"),
                ev.get("transactionHash"),
                now,
            ),
        )
        return cur.rowcount > 0
    except sqlite3.IntegrityError:
        return False


def _maybe_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _maybe_int(v) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def backfill_all_watchlist(
    conn: sqlite3.Connection,
    client: PolymarketClient,
    *,
    only_active: bool = True,
    **kwargs,
) -> dict:
    """Run backfill_wallet_activity for every watchlist entry."""
    sql = "SELECT wallet FROM whale_watchlist"
    if only_active:
        sql += " WHERE active = 1"
    wallets = [r["wallet"] for r in conn.execute(sql)]
    summary = {"wallets": len(wallets), "total_inserted": 0, "total_pages": 0}
    for w in wallets:
        result = backfill_wallet_activity(conn, client, w, **kwargs)
        summary["total_inserted"] += result["inserted"]
        summary["total_pages"] += result["pages"]
    return summary


# --- Market resolution caching ---


def get_or_fetch_resolution(
    conn: sqlite3.Connection,
    client: PolymarketClient,
    *,
    condition_id: str | None = None,
    market_slug: str | None = None,
    max_age_s: int = 7 * 86400,
) -> dict | None:
    """Look up cached market resolution, refetching if missing or stale.

    Returns dict with {closed, outcome_prices (list[float]), token_ids, title}
    or None if neither cache nor API has it.
    """
    now = int(time.time())
    cached = None
    if condition_id:
        cached = conn.execute(
            "SELECT * FROM market_resolutions WHERE condition_id = ?",
            (condition_id,),
        ).fetchone()
    if cached and (now - int(cached["last_refreshed"])) < max_age_s:
        return _resolution_to_dict(cached)
    if not market_slug and cached:
        market_slug = cached["market_slug"]
    if not market_slug:
        return _resolution_to_dict(cached) if cached else None
    try:
        market = client.get_market(market_slug)
    except Exception as exc:
        logger.warning("gamma fetch failed slug=%s: %s", market_slug, exc)
        return _resolution_to_dict(cached) if cached else None
    if market is None:
        return _resolution_to_dict(cached) if cached else None
    conn.execute(
        """
        INSERT OR REPLACE INTO market_resolutions(
            condition_id, market_slug, title, closed, outcome_prices,
            token_ids, end_date, fetched_at, last_refreshed
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            condition_id or "",
            market.slug,
            market.question,
            1 if market.closed else 0,
            json.dumps(market.outcome_prices),
            json.dumps(market.token_ids),
            market.end_date,
            int(cached["fetched_at"]) if cached else now,
            now,
        ),
    )
    conn.commit()
    return {
        "closed": bool(market.closed),
        "outcome_prices": list(market.outcome_prices),
        "token_ids": list(market.token_ids),
        "title": market.question,
        "market_slug": market.slug,
    }


def _resolution_to_dict(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    try:
        op = json.loads(row["outcome_prices"]) if row["outcome_prices"] else []
        ti = json.loads(row["token_ids"]) if row["token_ids"] else []
    except json.JSONDecodeError:
        op = []
        ti = []
    return {
        "closed": bool(row["closed"]),
        "outcome_prices": [float(x) for x in op],
        "token_ids": [str(x) for x in ti],
        "title": row["title"],
        "market_slug": row["market_slug"],
    }
