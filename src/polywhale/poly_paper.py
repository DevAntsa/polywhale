"""Polymarket paper trading.

Two entry points:
  - record_combo_arb_legs:  given a detected ComboArb, record one paper bet per
    leg sized so total stake matches the user-given budget. If sum_best_ask <
    1.00 this locks a paper profit; otherwise it's a known paper loss (useful
    as a smoke test).
  - record_single_leg:  manual / directional paper bet on one market+side.

Settlement reads market.closed + outcomePrices from Gamma. Winning side gets
$1 per share; losing side gets $0. P&L = (payout - entry) * shares.
"""

import json
import logging
import sqlite3
import time
from dataclasses import dataclass

from polywhale.poly_arb import ComboArb
from polywhale.polymarket import PolymarketClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PaperBetSummary:
    placed: int
    total_cost_usd: float


def record_combo_arb_legs(
    conn: sqlite3.Connection,
    arb: ComboArb,
    *,
    arb_id: int | None = None,
    total_stake_usd: float = 100.0,
    event_slug: str | None = None,
) -> PaperBetSummary:
    """Record one paper bet per leg of a combo arb.

    Sizing: buys N shares of each YES token where N is chosen so total cost equals
    `total_stake_usd`. N = total_stake_usd / sum_best_ask. If a single outcome wins,
    payout is N * $1 = N. Profit = N - total_stake_usd if sum_best_ask < 1.
    """
    if not arb.legs or arb.sum_best_ask <= 0:
        return PaperBetSummary(0, 0.0)
    shares_per_leg = total_stake_usd / arb.sum_best_ask
    ev_slug = event_slug or arb.event_slug
    now = int(time.time())
    placed = 0
    total_cost = 0.0
    for leg in arb.legs:
        if leg.best_ask is None or leg.best_ask <= 0:
            continue
        cost = leg.best_ask * shares_per_leg
        conn.execute(
            """
            INSERT INTO poly_paper_bets (
                source, source_ref_id, market_slug, event_slug, token_id,
                side, outcome_title, entry_price, size_shares, cost_usd, placed_at
            ) VALUES ('combo_arb', ?, ?, ?, ?, 'YES', ?, ?, ?, ?, ?)
            """,
            (
                arb_id,
                leg.market_slug,
                ev_slug,
                leg.token_id,
                leg.outcome_title,
                float(leg.best_ask),
                round(shares_per_leg, 6),
                round(cost, 4),
                now,
            ),
        )
        placed += 1
        total_cost += cost
    conn.commit()
    logger.info(
        "recorded combo_arb paper bets: %d legs, total cost $%.2f, arb_id=%s",
        placed,
        total_cost,
        arb_id,
    )
    return PaperBetSummary(placed, round(total_cost, 2))


def record_single_leg(
    conn: sqlite3.Connection,
    *,
    market_slug: str,
    event_slug: str | None,
    token_id: str,
    side: str,
    outcome_title: str | None,
    entry_price: float,
    size_shares: float,
    source: str = "manual",
    source_ref_id: int | None = None,
    notes: str | None = None,
) -> int:
    """Record one directional paper bet (e.g., copy-trade of a whale's leg)."""
    now = int(time.time())
    cost = entry_price * size_shares
    cur = conn.execute(
        """
        INSERT INTO poly_paper_bets (
            source, source_ref_id, market_slug, event_slug, token_id,
            side, outcome_title, entry_price, size_shares, cost_usd, placed_at, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source,
            source_ref_id,
            market_slug,
            event_slug,
            token_id,
            side,
            outcome_title,
            float(entry_price),
            float(size_shares),
            round(cost, 4),
            now,
            notes,
        ),
    )
    conn.commit()
    return cur.lastrowid or 0


def settle_paper_bets(conn: sqlite3.Connection, client: PolymarketClient) -> dict:
    """For each unsettled paper bet, poll Gamma for the market's closed flag and
    outcomePrices. Mark won/lost based on which outcome resolved to $1."""
    rows = conn.execute(
        "SELECT bet_id, market_slug, token_id, side, entry_price, size_shares "
        "FROM poly_paper_bets WHERE settled_at IS NULL"
    ).fetchall()
    if not rows:
        return {"checked": 0, "settled": 0, "still_open": 0}

    markets = {r["market_slug"] for r in rows}
    resolutions: dict[str, tuple[bool, list[float] | None]] = {}
    for slug in markets:
        market = client.get_market(slug)
        if market and market.closed and market.outcome_prices:
            resolutions[slug] = (True, market.outcome_prices)
        else:
            resolutions[slug] = (False, None)

    now = int(time.time())
    settled = 0
    still_open = 0
    for row in rows:
        closed, prices = resolutions.get(row["market_slug"], (False, None))
        if not closed or not prices:
            still_open += 1
            continue
        yes_won = bool(prices[0] >= 0.99)
        if row["side"] == "YES":
            payout = 1.0 if yes_won else 0.0
            outcome_label = "won" if yes_won else "lost"
        else:
            payout = 1.0 if not yes_won else 0.0
            outcome_label = "won" if not yes_won else "lost"
        pnl = (payout - float(row["entry_price"])) * float(row["size_shares"])
        conn.execute(
            "UPDATE poly_paper_bets SET settled_at = ?, resolved_outcome = ?, "
            "payout_per_share = ?, pnl_usd = ? WHERE bet_id = ?",
            (now, outcome_label, payout, round(pnl, 4), row["bet_id"]),
        )
        settled += 1
    conn.commit()
    summary = {
        "checked": len(markets),
        "settled": settled,
        "still_open": still_open,
    }
    logger.info("settle_paper_bets: %s", json.dumps(summary))
    return summary


def paper_pnl_summary(conn: sqlite3.Connection) -> dict:
    """Group P&L stats by source ('combo_arb', 'whale_copy', 'manual')."""
    out: dict = {}
    for row in conn.execute(
        """
        SELECT source,
               COUNT(*)                                AS bets,
               SUM(cost_usd)                            AS total_cost,
               SUM(CASE WHEN settled_at IS NOT NULL
                        THEN 1 ELSE 0 END)              AS settled_bets,
               SUM(CASE WHEN settled_at IS NOT NULL
                        THEN cost_usd ELSE 0 END)       AS settled_cost,
               SUM(COALESCE(pnl_usd, 0))                AS total_pnl,
               SUM(CASE WHEN resolved_outcome = 'won'
                        THEN 1 ELSE 0 END)              AS wins,
               SUM(CASE WHEN resolved_outcome = 'lost'
                        THEN 1 ELSE 0 END)              AS losses
        FROM poly_paper_bets
        GROUP BY source
        """
    ):
        out[row["source"]] = {
            "bets": row["bets"],
            "settled_bets": row["settled_bets"] or 0,
            "wins": row["wins"] or 0,
            "losses": row["losses"] or 0,
            "total_cost": round(row["total_cost"] or 0.0, 2),
            "settled_cost": round(row["settled_cost"] or 0.0, 2),
            "total_pnl": round(row["total_pnl"] or 0.0, 2),
        }
    return out
