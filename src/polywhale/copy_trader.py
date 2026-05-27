"""Auto paper-bet on whale signals + PnL on exits.

Hooked into whale-fast between persist_signals() and send_signal_alerts():

  - NEW or ADDED signal -> place_copy_bet(): record a paper_bet at the price
    observed when the signal fired, sized at `bankroll_usd * stake_pct *
    conviction_discount`. source='whale_copy', source_ref_id=signal_id.

  - EXIT or TRIM signal -> close_copy_bet(): find any open paper_bet with the
    same (wallet via source_ref_id linkage) + same asset_id, mark it settled
    at the new price, compute PnL = (exit_price - entry_price) * shares.
    closed_by_signal_id = the exiting signal so the alerter can fetch the PnL.

Sizing is fixed-$ times conviction (idea #10 from the strategy brainstorm).
Per-whale weighting (bigger size for proven WR) is Phase B once we have
real backtest data.

Limitation: if the bot wasn't running when the whale entered, there's no
open paper_bet to close — the EXIT alert just fires without a PnL line.
That's correct behavior.
"""

import logging
import sqlite3
import time

logger = logging.getLogger(__name__)

ENTRY_SIGNAL_TYPES = ("new_position", "added_size")
EXIT_SIGNAL_TYPES = ("closed_position", "reduced_size")


def process_copy_trades(
    conn: sqlite3.Connection,
    *,
    bankroll_usd: float,
    stake_pct: float,
    weight_by_conviction: bool = True,
) -> dict:
    """For each unalerted signal: open or close a paper copy bet.

    Idempotent: each signal is only processed once because we look for paper_bets
    already linked via source_ref_id or closed_by_signal_id.
    """
    rows = conn.execute(
        "SELECT * FROM whale_signals WHERE alerted_at IS NULL"
    ).fetchall()
    opened = 0
    closed = 0
    realized_pnl = 0.0
    for r in rows:
        sig = r["signal_type"]
        if sig in ENTRY_SIGNAL_TYPES:
            if place_copy_bet(
                conn, r,
                bankroll_usd=bankroll_usd,
                stake_pct=stake_pct,
                weight_by_conviction=weight_by_conviction,
            ):
                opened += 1
        elif sig in EXIT_SIGNAL_TYPES:
            pnl = close_copy_bet(conn, r)
            if pnl is not None:
                closed += 1
                realized_pnl += pnl
    return {"opened": opened, "closed": closed, "realized_pnl": round(realized_pnl, 4)}


def place_copy_bet(
    conn: sqlite3.Connection,
    signal_row: sqlite3.Row,
    *,
    bankroll_usd: float,
    stake_pct: float,
    weight_by_conviction: bool = True,
) -> bool:
    """Open a paper bet on the whale's outcome. Returns True if recorded."""
    existing = conn.execute(
        "SELECT 1 FROM poly_paper_bets "
        "WHERE source = 'whale_copy' AND source_ref_id = ?",
        (signal_row["signal_id"],),
    ).fetchone()
    if existing:
        return False
    price = signal_row["current_price"]
    if price is None or price <= 0 or price >= 1:
        return False
    market_slug = signal_row["market_slug"]
    asset_id = signal_row["asset_id"]
    if not market_slug or not asset_id:
        return False
    conviction = signal_row["conviction_discount"]
    conviction_f = float(conviction) if conviction is not None else 1.0
    multiplier = conviction_f if weight_by_conviction else 1.0
    stake = bankroll_usd * stake_pct * multiplier
    if stake <= 0:
        return False
    shares = stake / float(price)
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO poly_paper_bets(
            source, source_ref_id, market_slug, event_slug, token_id,
            side, outcome_title, entry_price, size_shares, cost_usd, placed_at,
            intended_shares, capacity_capped, notes
        ) VALUES ('whale_copy', ?, ?, NULL, ?, 'YES', ?, ?, ?, ?, ?, ?, 0, ?)
        """,
        (
            signal_row["signal_id"],
            market_slug,
            asset_id,
            signal_row["outcome"],
            float(price),
            round(shares, 6),
            round(stake, 4),
            now,
            round(shares, 6),
            f"copy_of_{signal_row['wallet'][:10]}",
        ),
    )
    conn.commit()
    logger.info(
        "place_copy_bet signal=%d wallet=%s stake=$%.2f shares=%.1f @ %.4f",
        signal_row["signal_id"], signal_row["wallet"][:10], stake, shares, price,
    )
    return True


def close_copy_bet(
    conn: sqlite3.Connection,
    signal_row: sqlite3.Row,
) -> float | None:
    """Find an open paper bet matching this whale + asset and close it. Returns PnL or None."""
    asset_id = signal_row["asset_id"]
    wallet = signal_row["wallet"]
    if not asset_id or not wallet:
        return None
    open_bet = conn.execute(
        """
        SELECT pb.* FROM poly_paper_bets pb
        JOIN whale_signals ws ON pb.source_ref_id = ws.signal_id
        WHERE pb.source = 'whale_copy'
          AND pb.settled_at IS NULL
          AND pb.frozen_at IS NULL
          AND ws.wallet = ?
          AND ws.asset_id = ?
        ORDER BY pb.placed_at DESC
        LIMIT 1
        """,
        (wallet, asset_id),
    ).fetchone()
    if not open_bet:
        return None
    exit_price = signal_row["current_price"]
    if exit_price is None or exit_price <= 0:
        return None
    entry_price = float(open_bet["entry_price"])
    shares = float(open_bet["size_shares"])
    pnl = (float(exit_price) - entry_price) * shares
    now = int(time.time())
    conn.execute(
        """
        UPDATE poly_paper_bets SET
            settled_at = ?,
            resolved_outcome = 'closed_early',
            payout_per_share = ?,
            pnl_usd = ?,
            closed_by_signal_id = ?
        WHERE bet_id = ?
        """,
        (
            now,
            float(exit_price),
            round(pnl, 4),
            signal_row["signal_id"],
            open_bet["bet_id"],
        ),
    )
    conn.commit()
    logger.info(
        "close_copy_bet signal=%d wallet=%s entry=%.4f exit=%.4f shares=%.1f pnl=$%.2f",
        signal_row["signal_id"], wallet[:10], entry_price, exit_price, shares, pnl,
    )
    return pnl


def find_open_copy_bet_for_signal(
    conn: sqlite3.Connection, signal_id: int
) -> sqlite3.Row | None:
    """Look up the paper bet a NEW/ADDED signal opened (for alert enrichment)."""
    return conn.execute(
        "SELECT * FROM poly_paper_bets "
        "WHERE source = 'whale_copy' AND source_ref_id = ?",
        (signal_id,),
    ).fetchone()


def find_closed_copy_bet_by_exit_signal(
    conn: sqlite3.Connection, signal_id: int
) -> sqlite3.Row | None:
    """Look up the paper bet an EXIT/TRIM signal closed (for alert enrichment)."""
    return conn.execute(
        "SELECT * FROM poly_paper_bets WHERE closed_by_signal_id = ?",
        (signal_id,),
    ).fetchone()


def copy_trade_stats(conn: sqlite3.Connection) -> dict:
    """Open + realized stats across all whale_copy paper bets."""
    open_row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd), 0) AS deployed "
        "FROM poly_paper_bets WHERE source = 'whale_copy' AND settled_at IS NULL"
    ).fetchone()
    closed_row = conn.execute(
        "SELECT COUNT(*) AS n, "
        "       COALESCE(SUM(pnl_usd), 0) AS pnl, "
        "       SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins, "
        "       SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END) AS losses "
        "FROM poly_paper_bets WHERE source = 'whale_copy' AND settled_at IS NOT NULL"
    ).fetchone()
    wins = int(closed_row["wins"] or 0)
    losses = int(closed_row["losses"] or 0)
    wr = (wins / (wins + losses) * 100.0) if (wins + losses) > 0 else None
    return {
        "open_positions": int(open_row["n"] or 0),
        "capital_deployed": round(float(open_row["deployed"] or 0), 2),
        "closed_positions": int(closed_row["n"] or 0),
        "realized_pnl": round(float(closed_row["pnl"] or 0), 2),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(wr, 1) if wr is not None else None,
    }
