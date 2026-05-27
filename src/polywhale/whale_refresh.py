"""Auto-discovery loop: keep the whale watchlist fresh as the leaderboard shifts.

Two layers:
  1. SEED — on first run, copy the manually-curated static lists from watchlist.py
     into the DB so the bot has a baseline. Idempotent across re-runs.
  2. REFRESH — pull the current leaderboard, classify each candidate by margin
     and PnL, upsert qualifying sharps into the DB. Deactivate previously
     auto-added wallets that have gone dormant (no whale_positions activity
     in `max_dormant_days`).

Manual entries (source='manual') are NEVER auto-deactivated. The user added
them deliberately; only an explicit `polywhale watchlist-remove` drops them.
"""

import logging
import sqlite3
import time
from dataclasses import dataclass

from polywhale.polymarket import PolymarketClient
from polywhale.watchlist import MARGIN_RANKED_SHARPS, POLYWHALER_SHARPS
from polywhale.whale_classify import SHAPE_SHARP, fetch_and_classify, persist_profiles

logger = logging.getLogger(__name__)

SOURCE_MANUAL = "manual"
SOURCE_AUTO_MARGIN = "auto-margin"


@dataclass(frozen=True)
class RefreshResult:
    seeded: int
    added: int
    updated: int
    deactivated: int
    active_total: int


def seed_from_static(conn: sqlite3.Connection) -> int:
    """One-time copy of static watchlist constants into the DB. Idempotent."""
    now = int(time.time())
    wallets = list(dict.fromkeys(MARGIN_RANKED_SHARPS + POLYWHALER_SHARPS))
    inserted = 0
    for w in wallets:
        cur = conn.execute(
            "INSERT OR IGNORE INTO whale_watchlist(wallet, source, added_at, active) "
            "VALUES (?, ?, ?, 1)",
            (w.lower(), SOURCE_MANUAL, now),
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted


def refresh_watchlist(
    conn: sqlite3.Connection,
    client: PolymarketClient,
    *,
    min_margin_pct: float = 3.0,
    min_profit_usd: float = 50_000.0,
    min_volume_usd: float = 1_000_000.0,
    max_dormant_days: int = 14,
    window: str = "30d",
    top_n: int = 100,
) -> RefreshResult:
    """Pull leaderboard, upsert qualifying sharps, deactivate dormant auto entries."""
    count = conn.execute("SELECT COUNT(*) FROM whale_watchlist").fetchone()[0]
    seeded = seed_from_static(conn) if count == 0 else 0

    profiles = fetch_and_classify(
        client, window=window, top_n=top_n, min_volume=min_volume_usd
    )
    persist_profiles(conn, profiles)

    now = int(time.time())
    added = 0
    updated = 0
    for p in profiles:
        if p.shape != SHAPE_SHARP:
            continue
        if p.margin_pct < min_margin_pct:
            continue
        if p.profit < min_profit_usd:
            continue
        wallet = p.wallet.lower()
        existing = conn.execute(
            "SELECT wallet FROM whale_watchlist WHERE wallet = ?", (wallet,)
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE whale_watchlist SET
                    label = COALESCE(?, label),
                    margin_pct = ?,
                    profit_usd = ?,
                    volume_usd = ?,
                    shape = ?,
                    last_classified_at = ?,
                    active = 1,
                    deactivated_at = NULL,
                    deactivated_reason = NULL
                WHERE wallet = ?
                """,
                (
                    p.pseudonym or p.name,
                    p.margin_pct,
                    p.profit,
                    p.volume,
                    p.shape,
                    now,
                    wallet,
                ),
            )
            updated += 1
        else:
            conn.execute(
                """
                INSERT INTO whale_watchlist(
                    wallet, label, source, added_at, last_classified_at,
                    margin_pct, profit_usd, volume_usd, shape, active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    wallet,
                    p.pseudonym or p.name,
                    SOURCE_AUTO_MARGIN,
                    now,
                    now,
                    p.margin_pct,
                    p.profit,
                    p.volume,
                    p.shape,
                ),
            )
            added += 1
    conn.commit()

    update_activity_stats(conn)
    deactivated = mark_dormant_auto(conn, days=max_dormant_days)

    active_total = conn.execute(
        "SELECT COUNT(*) FROM whale_watchlist WHERE active = 1"
    ).fetchone()[0]

    return RefreshResult(
        seeded=seeded,
        added=added,
        updated=updated,
        deactivated=deactivated,
        active_total=int(active_total),
    )


def update_activity_stats(conn: sqlite3.Connection, *, window_days: int = 30) -> int:
    """Refresh signals_30d + last_signal_at on every watchlist row from whale_signals."""
    cutoff = int(time.time()) - window_days * 86400
    # Reset all to 0 first so wallets with no recent signals get cleared correctly.
    conn.execute("UPDATE whale_watchlist SET signals_30d = 0")
    rows = conn.execute(
        """
        SELECT wallet, COUNT(*) AS n, MAX(detected_at) AS last_ts
        FROM whale_signals
        WHERE detected_at >= ?
        GROUP BY wallet
        """,
        (cutoff,),
    ).fetchall()
    updated = 0
    for r in rows:
        cur = conn.execute(
            "UPDATE whale_watchlist SET signals_30d = ?, last_signal_at = ? WHERE wallet = ?",
            (int(r["n"]), int(r["last_ts"]), r["wallet"]),
        )
        updated += cur.rowcount
    conn.commit()
    return updated


def mark_dormant_auto(conn: sqlite3.Connection, *, days: int) -> int:
    """Deactivate auto-discovered entries with no whale_positions activity in N days.

    Manual entries are excluded — those stay active even if dormant.
    Newly-added wallets get a grace period of `days`: deactivation only kicks in
    after they've been on the watchlist long enough to have a chance to be polled.
    """
    now = int(time.time())
    cutoff = now - days * 86400
    cur = conn.execute(
        """
        UPDATE whale_watchlist
        SET active = 0,
            deactivated_at = ?,
            deactivated_reason = ?
        WHERE active = 1
          AND source != ?
          AND added_at < ?
          AND wallet NOT IN (
              SELECT DISTINCT wallet FROM whale_positions WHERE captured_at >= ?
          )
        """,
        (now, f"dormant > {days}d", SOURCE_MANUAL, cutoff, cutoff),
    )
    conn.commit()
    return cur.rowcount


def load_active_watchlist(conn: sqlite3.Connection) -> list[str]:
    """Active wallets from DB, sorted by recent activity then profit.
    Falls back to static union if DB is empty (transition-safe).
    """
    rows = conn.execute(
        "SELECT wallet FROM whale_watchlist WHERE active = 1 "
        "ORDER BY signals_30d DESC, profit_usd DESC NULLS LAST"
    ).fetchall()
    if rows:
        return [r["wallet"] for r in rows]
    return list(dict.fromkeys(MARGIN_RANKED_SHARPS + POLYWHALER_SHARPS))


def upsert_manual(
    conn: sqlite3.Connection, *, wallet: str, label: str | None = None, notes: str | None = None
) -> bool:
    """Add or re-activate a manual watchlist entry. Returns True if a row was created or changed."""
    now = int(time.time())
    cur = conn.execute(
        """
        INSERT INTO whale_watchlist(wallet, label, source, added_at, active, notes)
        VALUES (?, ?, ?, ?, 1, ?)
        ON CONFLICT(wallet) DO UPDATE SET
            active = 1,
            deactivated_at = NULL,
            deactivated_reason = NULL,
            label = COALESCE(excluded.label, whale_watchlist.label),
            notes = COALESCE(excluded.notes, whale_watchlist.notes)
        """,
        (wallet.lower(), label, SOURCE_MANUAL, now, notes),
    )
    conn.commit()
    return cur.rowcount > 0


def deactivate(conn: sqlite3.Connection, wallet: str, *, reason: str = "manual") -> bool:
    cur = conn.execute(
        "UPDATE whale_watchlist SET active = 0, deactivated_at = ?, deactivated_reason = ? "
        "WHERE wallet = ? AND active = 1",
        (int(time.time()), reason, wallet.lower()),
    )
    conn.commit()
    return cur.rowcount > 0
