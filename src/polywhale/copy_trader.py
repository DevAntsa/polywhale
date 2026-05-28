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

from polywhale.ai_advisor import (
    AIAdvice,
    build_context_from_signal,
    call_advisor,
)

logger = logging.getLogger(__name__)

ENTRY_SIGNAL_TYPES = ("new_position", "added_size")
EXIT_SIGNAL_TYPES = ("closed_position", "reduced_size")


def current_deployed_usd(conn: sqlite3.Connection) -> float:
    """Sum of cost_usd across open whale_copy paper bets."""
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM poly_paper_bets "
        "WHERE source = 'whale_copy' AND settled_at IS NULL"
    ).fetchone()
    return float(row[0] or 0)


def process_copy_trades(
    conn: sqlite3.Connection,
    *,
    bankroll_usd: float,
    stake_pct: float,
    weight_by_conviction: bool = True,
    ai_api_key: str = "",
    ai_model: str = "",
    use_ai_advisor: bool = False,
) -> dict:
    """For each unalerted signal: open or close a paper copy bet.

    Idempotent: each signal is only processed once because we look for paper_bets
    already linked via source_ref_id or closed_by_signal_id.

    If use_ai_advisor is True, ENTRY signals also call the OpenRouter advisor to
    get a stake multiplier on top of the mechanical conviction-weighted base.
    """
    rows = conn.execute(
        "SELECT * FROM whale_signals WHERE alerted_at IS NULL"
    ).fetchall()
    opened = 0
    closed = 0
    skipped_bankroll = 0
    realized_pnl = 0.0
    ai_calls = 0
    for r in rows:
        sig = r["signal_type"]
        if sig in ENTRY_SIGNAL_TYPES:
            ai_advice = None
            if use_ai_advisor and ai_api_key:
                try:
                    ctx = build_context_from_signal(conn, r, bankroll=bankroll_usd)
                    ai_advice = call_advisor(
                        context=ctx, api_key=ai_api_key, model=ai_model,
                    )
                    ai_calls += 1
                except Exception as exc:
                    logger.warning(
                        "ai_advisor failed for signal %s: %s — falling back to 1.0x",
                        r["signal_id"], exc,
                    )
                    ai_advice = None
            deployed_before = current_deployed_usd(conn)
            if place_copy_bet(
                conn, r,
                bankroll_usd=bankroll_usd,
                stake_pct=stake_pct,
                weight_by_conviction=weight_by_conviction,
                ai_advice=ai_advice,
            ):
                opened += 1
            else:
                # If deployed >= bankroll, this skip was bankroll-driven.
                if deployed_before >= bankroll_usd * 0.99:
                    skipped_bankroll += 1
        elif sig in EXIT_SIGNAL_TYPES:
            pnl = close_copy_bet(conn, r)
            if pnl is not None:
                closed += 1
                realized_pnl += pnl
    return {
        "opened": opened,
        "closed": closed,
        "skipped_bankroll": skipped_bankroll,
        "realized_pnl": round(realized_pnl, 4),
        "ai_calls": ai_calls,
    }


def place_copy_bet(
    conn: sqlite3.Connection,
    signal_row: sqlite3.Row,
    *,
    bankroll_usd: float,
    stake_pct: float,
    weight_by_conviction: bool = True,
    ai_advice: AIAdvice | None = None,
) -> bool:
    """Open a paper bet on the whale's outcome. Returns True if recorded.

    Mechanical stake = bankroll * stake_pct * conviction_discount.
    If ai_advice is provided, final stake = mechanical * ai_advice.multiplier.
    Both are stored so we can A/B-compare PnL later.
    """
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
    conv_mult = conviction_f if weight_by_conviction else 1.0
    mechanical_stake = bankroll_usd * stake_pct * conv_mult
    ai_mult = ai_advice.multiplier if ai_advice is not None else 1.0
    final_stake = mechanical_stake * ai_mult
    if final_stake <= 0:
        return False
    deployed = current_deployed_usd(conn)
    if deployed + final_stake > bankroll_usd:
        logger.info(
            "place_copy_bet skipped (bankroll full): deployed=$%.2f + new=$%.2f > $%.2f",
            deployed, final_stake, bankroll_usd,
        )
        return False
    shares = final_stake / float(price)
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO poly_paper_bets(
            source, source_ref_id, market_slug, event_slug, token_id,
            side, outcome_title, entry_price, size_shares, cost_usd, placed_at,
            intended_shares, capacity_capped, notes,
            mechanical_stake, ai_multiplier, ai_reason, ai_confidence
        ) VALUES ('whale_copy', ?, ?, NULL, ?, 'YES', ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
        """,
        (
            signal_row["signal_id"],
            market_slug,
            asset_id,
            signal_row["outcome"],
            float(price),
            round(shares, 6),
            round(final_stake, 4),
            now,
            round(shares, 6),
            f"copy_of_{signal_row['wallet'][:10]}",
            round(mechanical_stake, 4),
            ai_advice.multiplier if ai_advice else None,
            ai_advice.reason if ai_advice else None,
            ai_advice.confidence if ai_advice else None,
        ),
    )
    conn.commit()
    logger.info(
        "place_copy_bet signal=%d wallet=%s stake=$%.2f (mech=$%.2f x ai=%.2f) "
        "shares=%.1f @ %.4f",
        signal_row["signal_id"], signal_row["wallet"][:10], final_stake,
        mechanical_stake, ai_mult, shares, price,
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
