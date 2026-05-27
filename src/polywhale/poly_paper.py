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
    capacity_capped: bool = False


# Yang et al. (arXiv 2605.00864) measured median executable combo-arb leg at
# ~14.8 shares; depth_within(5%) overstates real fill so we leave a buffer.
CAPACITY_BUFFER = 0.8


def record_combo_arb_legs(
    conn: sqlite3.Connection,
    arb: ComboArb,
    *,
    arb_id: int | None = None,
    total_stake_usd: float = 100.0,
    event_slug: str | None = None,
) -> PaperBetSummary:
    """Record one paper bet per leg of a combo arb.

    Sizing: target `total_stake_usd / sum_best_ask` shares per leg, then cap by the
    shallowest leg's observed ask depth so paper sizes can actually fill in production.
    """
    if not arb.legs or arb.sum_best_ask <= 0:
        return PaperBetSummary(0, 0.0)
    intended_shares = total_stake_usd / arb.sum_best_ask
    depths = [leg.ask_depth for leg in arb.legs if leg.ask_depth and leg.ask_depth > 0]
    min_depth = min(depths) if depths else None
    if min_depth is not None and intended_shares > min_depth * CAPACITY_BUFFER:
        shares_per_leg = min_depth * CAPACITY_BUFFER
        capped = True
    else:
        shares_per_leg = intended_shares
        capped = False
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
                side, outcome_title, entry_price, size_shares, cost_usd, placed_at,
                intended_shares, capacity_capped
            ) VALUES ('combo_arb', ?, ?, ?, ?, 'YES', ?, ?, ?, ?, ?, ?, ?)
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
                round(intended_shares, 6),
                1 if capped else 0,
            ),
        )
        placed += 1
        total_cost += cost
    conn.commit()
    logger.info(
        "recorded combo_arb paper bets: %d legs, cost $%.2f, capped=%s, arb_id=%s",
        placed,
        total_cost,
        capped,
        arb_id,
    )
    return PaperBetSummary(placed, round(total_cost, 2), capped)


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


def freeze_paper_bet(
    conn: sqlite3.Connection, bet_id: int, *, reason: str
) -> bool:
    """Freeze a paper bet so settlement skips it (e.g., market is under UMA dispute).

    Returns True if the row was updated. Idempotent — refreezing is a no-op.
    """
    now = int(time.time())
    cur = conn.execute(
        "UPDATE poly_paper_bets SET frozen_at = ?, frozen_reason = ? "
        "WHERE bet_id = ? AND frozen_at IS NULL AND settled_at IS NULL",
        (now, reason, bet_id),
    )
    conn.commit()
    return cur.rowcount > 0


def unfreeze_paper_bet(conn: sqlite3.Connection, bet_id: int) -> bool:
    """Clear the frozen flag (e.g., UMA dispute resolved in favor of original outcome)."""
    cur = conn.execute(
        "UPDATE poly_paper_bets SET frozen_at = NULL, frozen_reason = NULL "
        "WHERE bet_id = ? AND frozen_at IS NOT NULL",
        (bet_id,),
    )
    conn.commit()
    return cur.rowcount > 0


def settle_paper_bets(conn: sqlite3.Connection, client: PolymarketClient) -> dict:
    """For each unsettled, unfrozen paper bet, poll Gamma for the market's closed flag
    and outcomePrices. Mark won/lost based on which outcome resolved to $1."""
    rows = conn.execute(
        "SELECT bet_id, market_slug, token_id, side, entry_price, size_shares "
        "FROM poly_paper_bets WHERE settled_at IS NULL AND frozen_at IS NULL"
    ).fetchall()
    if not rows:
        frozen = conn.execute(
            "SELECT COUNT(*) FROM poly_paper_bets "
            "WHERE frozen_at IS NOT NULL AND settled_at IS NULL"
        ).fetchone()[0]
        return {"checked": 0, "settled": 0, "still_open": 0, "frozen": int(frozen or 0)}

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
    frozen = conn.execute(
        "SELECT COUNT(*) FROM poly_paper_bets WHERE frozen_at IS NOT NULL AND settled_at IS NULL"
    ).fetchone()[0]
    summary = {
        "checked": len(markets),
        "settled": settled,
        "still_open": still_open,
        "frozen": int(frozen or 0),
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
