"""Detect position changes between consecutive whale snapshots.

A snapshot pass writes many rows (one per position) all with the same captured_at.
Two consecutive passes -> we can diff them to find:

- `new_position`     : asset present at latest, missing at prev
- `added_size`       : size grew by >= add_threshold (default 50%)
- `closed_position`  : asset present at prev, missing at latest (size went to 0)
- `reduced_size`     : size dropped by >= reduce_threshold (default 50%) but not closed

Only emits a signal if the *change* is meaningful; small fluctuations are ignored.
"""

import logging
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

SIG_NEW = "new_position"
SIG_ADDED = "added_size"
SIG_CLOSED = "closed_position"
SIG_REDUCED = "reduced_size"


@dataclass(frozen=True)
class WhaleSignal:
    wallet: str
    signal_type: str
    asset_id: str | None
    market_slug: str | None
    title: str | None
    outcome: str | None
    old_size: float | None
    new_size: float | None
    current_price: float | None
    prev_captured_at: int | None
    latest_captured_at: int
    recent_move_pct: float | None = None
    conviction_discount: float | None = None


# McDonald, Tsang, Johnson, Galanis (2019) found prediction markets overreact
# to recent price moves. We fade signals that the market already chased.
OVERREACTION_LOOKBACK_SEC = 24 * 60 * 60
OVERREACTION_TRIGGER_PP = 0.05
OVERREACTION_FLOOR = 0.5


def _last_two_timestamps(conn: sqlite3.Connection, wallet: str) -> tuple[int, int] | None:
    rows = conn.execute(
        "SELECT DISTINCT captured_at FROM whale_positions WHERE wallet = ? "
        "ORDER BY captured_at DESC LIMIT 2",
        (wallet,),
    ).fetchall()
    if len(rows) < 2:
        return None
    return rows[0][0], rows[1][0]


def _positions_at(conn: sqlite3.Connection, wallet: str, ts: int) -> dict[str, sqlite3.Row]:
    return {
        row["asset_id"]: row
        for row in conn.execute(
            "SELECT * FROM whale_positions WHERE wallet = ? AND captured_at = ?",
            (wallet, ts),
        )
    }


def detect_signals_for_wallet(
    conn: sqlite3.Connection,
    wallet: str,
    *,
    add_threshold: float = 1.5,
    reduce_threshold: float = 0.5,
    min_size: float = 100.0,
) -> list[WhaleSignal]:
    """Compare the two most recent snapshot passes for a wallet.

    `add_threshold`: ratio new/old above which 'added_size' fires (1.5 = 50% growth).
    `reduce_threshold`: ratio new/old below which 'reduced_size' fires (0.5 = halved).
    `min_size`: ignore positions with absolute size below this (filters dust).
    """
    pair = _last_two_timestamps(conn, wallet)
    if pair is None:
        return []
    latest_ts, prev_ts = pair
    latest = _positions_at(conn, wallet, latest_ts)
    prev = _positions_at(conn, wallet, prev_ts)

    signals: list[WhaleSignal] = []

    for asset_id, row in latest.items():
        size = float(row["size"] or 0)
        if size < min_size:
            continue
        prev_row = prev.get(asset_id)
        if prev_row is None:
            signals.append(
                _signal_with_overreaction(
                    conn, SIG_NEW, row, None, size, prev_ts, latest_ts
                )
            )
            continue
        old_size = float(prev_row["size"] or 0)
        if old_size <= 0:
            continue
        if size >= old_size * add_threshold:
            signals.append(
                _signal_with_overreaction(
                    conn, SIG_ADDED, row, old_size, size, prev_ts, latest_ts
                )
            )

    for asset_id, prev_row in prev.items():
        old_size = float(prev_row["size"] or 0)
        if old_size < min_size:
            continue
        if asset_id not in latest:
            signals.append(_signal(SIG_CLOSED, prev_row, old_size, 0.0, prev_ts, latest_ts))
            continue
        new_size = float(latest[asset_id]["size"] or 0)
        if 0 < new_size <= old_size * reduce_threshold:
            signals.append(
                _signal(SIG_REDUCED, latest[asset_id], old_size, new_size, prev_ts, latest_ts)
            )
    return signals


def recent_price_move(
    conn: sqlite3.Connection,
    asset_id: str,
    since_ts: int,
) -> float | None:
    """Return last_price - first_price over polymarket_books for `asset_id` since `since_ts`.

    None if we have <2 snapshots in the window. The book table is keyed on token_id,
    which Polymarket uses interchangeably with asset_id.
    """
    rows = conn.execute(
        """
        SELECT best_ask, captured_at FROM polymarket_books
        WHERE token_id = ? AND captured_at >= ?
        ORDER BY captured_at ASC
        """,
        (asset_id, since_ts),
    ).fetchall()
    if len(rows) < 2:
        return None
    first_price = rows[0]["best_ask"]
    last_price = rows[-1]["best_ask"]
    if first_price is None or last_price is None:
        return None
    return float(last_price) - float(first_price)


def conviction_discount_from_move(
    move: float | None,
    *,
    current_price: float | None,
) -> float:
    """Map a 24h price move to a 0.5-1.0 conviction discount factor.

    A whale buying when the market has already moved >5pp in their direction is
    likely chasing. Discount to 0.5 at >=10pp move; linear in between.
    """
    if move is None:
        return 1.0
    if current_price is None:
        return 1.0
    pp_move = abs(move)
    if pp_move < OVERREACTION_TRIGGER_PP:
        return 1.0
    span = OVERREACTION_TRIGGER_PP
    excess = min(pp_move - OVERREACTION_TRIGGER_PP, span)
    return 1.0 - (1.0 - OVERREACTION_FLOOR) * (excess / span)


def _signal_with_overreaction(
    conn: sqlite3.Connection,
    signal_type: str,
    row: sqlite3.Row,
    old_size: float | None,
    new_size: float,
    prev_ts: int,
    latest_ts: int,
) -> WhaleSignal:
    asset_id = row["asset_id"]
    since_ts = latest_ts - OVERREACTION_LOOKBACK_SEC
    move = recent_price_move(conn, asset_id, since_ts) if asset_id else None
    current_price = row["current_price"] if "current_price" in row.keys() else None
    discount = conviction_discount_from_move(move, current_price=current_price)
    return WhaleSignal(
        wallet=row["wallet"],
        signal_type=signal_type,
        asset_id=asset_id,
        market_slug=row["market_slug"],
        title=row["title"],
        outcome=row["outcome"],
        old_size=old_size,
        new_size=new_size,
        current_price=current_price,
        prev_captured_at=prev_ts,
        latest_captured_at=latest_ts,
        recent_move_pct=move,
        conviction_discount=discount,
    )


def detect_for_wallets(
    conn: sqlite3.Connection,
    wallets: Iterable[str],
    **kwargs,
) -> list[WhaleSignal]:
    """Detect signals across multiple wallets in one call."""
    out: list[WhaleSignal] = []
    for wallet in wallets:
        out.extend(detect_signals_for_wallet(conn, wallet, **kwargs))
    return out


def persist_signals(conn: sqlite3.Connection, signals: list[WhaleSignal]) -> int:
    """Append signals to whale_signals. Dedup against existing (wallet, asset_id,
    signal_type, latest_captured_at)."""
    if not signals:
        return 0
    now = int(time.time())
    rows_to_insert = []
    for s in signals:
        existing = conn.execute(
            """
            SELECT 1 FROM whale_signals
            WHERE wallet = ? AND asset_id = ? AND signal_type = ?
              AND latest_captured_at = ?
            """,
            (s.wallet, s.asset_id, s.signal_type, s.latest_captured_at),
        ).fetchone()
        if existing:
            continue
        rows_to_insert.append(
            (
                s.wallet,
                s.signal_type,
                s.asset_id,
                s.market_slug,
                s.title,
                s.outcome,
                s.old_size,
                s.new_size,
                s.current_price,
                s.prev_captured_at,
                s.latest_captured_at,
                now,
                s.recent_move_pct,
                s.conviction_discount,
            )
        )
    if not rows_to_insert:
        return 0
    conn.executemany(
        """
        INSERT INTO whale_signals (
            wallet, signal_type, asset_id, market_slug, title, outcome,
            old_size, new_size, current_price, prev_captured_at,
            latest_captured_at, detected_at, recent_move_pct, conviction_discount
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows_to_insert,
    )
    conn.commit()
    logger.info("persisted %d whale signals", len(rows_to_insert))
    return len(rows_to_insert)


def _signal(
    signal_type: str,
    row: sqlite3.Row,
    old_size: float | None,
    new_size: float,
    prev_ts: int,
    latest_ts: int,
) -> WhaleSignal:
    return WhaleSignal(
        wallet=row["wallet"],
        signal_type=signal_type,
        asset_id=row["asset_id"],
        market_slug=row["market_slug"],
        title=row["title"],
        outcome=row["outcome"],
        old_size=old_size,
        new_size=new_size,
        current_price=row["current_price"] if "current_price" in row.keys() else None,
        prev_captured_at=prev_ts,
        latest_captured_at=latest_ts,
    )
