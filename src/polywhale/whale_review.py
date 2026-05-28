"""Decision system for whether to keep or drop a tracked whale.

The proper signal is OUR observed paper PnL per whale (from poly_paper_bets
closed via close_copy_bet), not external WR. External WR tells us a whale
makes money; our PnL tells us if WE can capture that money. Those differ
because of cadence, fills, market resolution timing, etc.

Tier definitions (computed per whale):

  A — proven contributor: realized_pnl_30d >= boost_threshold over >= boost_min_trades
                          → keep, and eventually boost stake size (future Phase 2)
  B — positive:           realized_pnl_all > 0 with any sample, OR
                          closed_trades_all < min_trades_to_judge (too early)
                          AND last_signal_at within max_quiet_days
                          → keep, no change
  C — watch:              closed_trades_all < min_trades_to_judge AND signals_30d > 0
                          → keep but flag as small sample
  D — drop:               closed_trades_all >= min_trades_to_judge AND
                          (realized_pnl_all <= loss_threshold OR
                           abs(avg_pnl) <= zero_pnl_epsilon)
                          → drop (proven negative or no-alpha contribution)
  E — dormant:            last_signal_at older than max_quiet_days
                          AND closed_trades_all < min_trades_to_judge
                          → drop (no data and no signals coming)

Manual entries (source='manual') are flagged with their tier but NEVER auto-dropped.
The user added them deliberately; only an explicit `watchlist-remove` drops them.
"""

import logging
import sqlite3
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

TIER_KEEP_BOOST = "A"
TIER_KEEP = "B"
TIER_WATCH = "C"
TIER_DROP = "D"
TIER_DROP_DORMANT = "E"

REC_KEEP_BOOST = "keep_boost"
REC_KEEP = "keep"
REC_WATCH = "watch"
REC_DROP = "drop"
REC_DROP_DORMANT = "drop_dormant"

DROPPABLE = (REC_DROP, REC_DROP_DORMANT)


@dataclass(frozen=True)
class WhaleReview:
    wallet: str
    label: str | None
    source: str
    # Observed (our closed paper copy bets)
    closed_trades_all: int
    closed_trades_30d: int
    realized_pnl_all: float
    realized_pnl_30d: float
    avg_pnl_per_trade: float | None
    wins: int
    losses: int
    # External
    margin_pct: float | None
    external_profit_usd: float | None
    # Activity
    last_signal_at: int | None
    last_trade_at: int | None
    signals_30d: int
    # Decision
    tier: str
    recommendation: str
    reason: str


def evaluate_whale(
    conn: sqlite3.Connection,
    wallet: str,
    *,
    min_trades_to_judge: int = 25,
    loss_threshold: float = -30.0,
    zero_pnl_epsilon: float = 0.20,
    max_quiet_days: int = 21,
    keep_threshold: float = 50.0,
    boost_threshold: float = 200.0,
    boost_min_trades: int = 25,
    now_ts: int | None = None,
) -> WhaleReview | None:
    """Compute tier + recommendation for one wallet. Returns None if not on watchlist."""
    now = now_ts if now_ts is not None else int(time.time())
    wl = conn.execute(
        "SELECT label, source, margin_pct, profit_usd, signals_30d, "
        "last_signal_at, last_trade_at, added_at "
        "FROM whale_watchlist WHERE wallet = ?",
        (wallet,),
    ).fetchone()
    if wl is None:
        return None

    # Observed paper-copy PnL for this wallet (matches via source_ref_id → whale_signals)
    cutoff_30d = now - 30 * 86400
    closed_all_row = conn.execute(
        """
        SELECT COUNT(*) AS n,
               COALESCE(SUM(pb.pnl_usd), 0) AS pnl,
               SUM(CASE WHEN pb.pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN pb.pnl_usd <= 0 THEN 1 ELSE 0 END) AS losses
        FROM poly_paper_bets pb
        JOIN whale_signals ws ON pb.source_ref_id = ws.signal_id
        WHERE pb.source = 'whale_copy'
          AND pb.settled_at IS NOT NULL
          AND ws.wallet = ?
        """,
        (wallet,),
    ).fetchone()
    closed_30d_row = conn.execute(
        """
        SELECT COUNT(*) AS n, COALESCE(SUM(pb.pnl_usd), 0) AS pnl
        FROM poly_paper_bets pb
        JOIN whale_signals ws ON pb.source_ref_id = ws.signal_id
        WHERE pb.source = 'whale_copy'
          AND pb.settled_at IS NOT NULL
          AND pb.settled_at >= ?
          AND ws.wallet = ?
        """,
        (cutoff_30d, wallet),
    ).fetchone()

    closed_all = int(closed_all_row["n"] or 0)
    closed_30d = int(closed_30d_row["n"] or 0)
    pnl_all = float(closed_all_row["pnl"] or 0)
    pnl_30d = float(closed_30d_row["pnl"] or 0)
    wins = int(closed_all_row["wins"] or 0)
    losses = int(closed_all_row["losses"] or 0)
    avg_pnl = (pnl_all / closed_all) if closed_all > 0 else None

    last_signal_at = wl["last_signal_at"]
    added_at = int(wl["added_at"]) if wl["added_at"] else now
    # Use the more recent of last_signal_at and added_at so newly-added wallets
    # get a grace period equal to max_quiet_days before they can be marked dormant.
    activity_anchor = max(int(last_signal_at) if last_signal_at else 0, added_at)
    quiet_days = (now - activity_anchor) / 86400.0 if activity_anchor else float("inf")

    # Tier resolution
    tier = TIER_WATCH
    recommendation = REC_WATCH
    reason = "default"

    is_dormant = quiet_days > max_quiet_days
    has_enough_samples = closed_all >= min_trades_to_judge

    if has_enough_samples and pnl_all >= boost_threshold and closed_all >= boost_min_trades:
        tier = TIER_KEEP_BOOST
        recommendation = REC_KEEP_BOOST
        reason = f"proven contributor (${pnl_all:+.2f} over {closed_all} trades)"
    elif has_enough_samples and pnl_all <= loss_threshold:
        tier = TIER_DROP
        recommendation = REC_DROP
        reason = f"net loss ${pnl_all:+.2f} over {closed_all} trades"
    elif (
        has_enough_samples
        and avg_pnl is not None
        and abs(avg_pnl) <= zero_pnl_epsilon
    ):
        tier = TIER_DROP
        recommendation = REC_DROP
        reason = (
            f"no captured alpha (avg ${avg_pnl:+.2f}/trade over {closed_all} trades)"
        )
    elif is_dormant and not has_enough_samples:
        tier = TIER_DROP_DORMANT
        recommendation = REC_DROP_DORMANT
        reason = f"dormant {quiet_days:.0f}d with no track record"
    elif has_enough_samples and pnl_all > 0:
        tier = TIER_KEEP
        recommendation = REC_KEEP
        reason = f"positive (${pnl_all:+.2f} over {closed_all} trades)"
    elif not has_enough_samples:
        tier = TIER_WATCH
        recommendation = REC_WATCH
        reason = f"too early ({closed_all} trades, need {min_trades_to_judge})"
    else:
        tier = TIER_KEEP
        recommendation = REC_KEEP
        reason = "default keep"

    # Manual entries can be flagged but never auto-dropped
    if wl["source"] == "manual" and recommendation in (REC_DROP, REC_DROP_DORMANT):
        # Keep the tier letter (D/E) so user sees the diagnosis, but switch
        # recommendation to "watch" so the auto-drop sweep skips them.
        recommendation = REC_WATCH
        reason = f"{reason} [manual — not auto-dropped]"

    # Endorsed wallets are NEVER auto-dropped regardless of source. Some genuine
    # alpha strategies (e.g. wokerjoesleeper's 81% WR low-prob-NOs algo
    # endorsed by Polymarket's official 26-address smart-money list) don't
    # surface PnL at our 60s polling cadence.
    is_endorsed = "endorsed" in wl.keys() and wl["endorsed"]
    if is_endorsed and recommendation in (REC_DROP, REC_DROP_DORMANT):
        endorsement_src = (
            wl["endorsement_source"] if "endorsement_source" in wl.keys() else "endorsed"
        )
        recommendation = REC_WATCH
        reason = f"{reason} [endorsed: {endorsement_src} — not auto-dropped]"

    return WhaleReview(
        wallet=wallet,
        label=wl["label"],
        source=wl["source"],
        closed_trades_all=closed_all,
        closed_trades_30d=closed_30d,
        realized_pnl_all=round(pnl_all, 2),
        realized_pnl_30d=round(pnl_30d, 2),
        avg_pnl_per_trade=round(avg_pnl, 4) if avg_pnl is not None else None,
        wins=wins,
        losses=losses,
        margin_pct=wl["margin_pct"],
        external_profit_usd=wl["profit_usd"],
        last_signal_at=last_signal_at,
        last_trade_at=wl["last_trade_at"],
        signals_30d=int(wl["signals_30d"] or 0),
        tier=tier,
        recommendation=recommendation,
        reason=reason,
    )


def evaluate_all_active(
    conn: sqlite3.Connection, **kwargs
) -> list[WhaleReview]:
    """Run evaluate_whale for every active watchlist entry."""
    wallets = [
        r["wallet"]
        for r in conn.execute(
            "SELECT wallet FROM whale_watchlist WHERE active = 1"
        )
    ]
    out: list[WhaleReview] = []
    for w in wallets:
        rev = evaluate_whale(conn, w, **kwargs)
        if rev is not None:
            out.append(rev)
    return out


def review_and_autodrop(
    conn: sqlite3.Connection, **kwargs
) -> list[WhaleReview]:
    """One-shot: evaluate all active whales, auto-drop droppable ones, return the
    drop list (full WhaleReview objects, useful for Telegram alerts).

    Manual entries are exempted in evaluate_whale; they never appear in the
    returned drop list.
    """
    reviews = evaluate_all_active(conn, **kwargs)
    droppable = [r for r in reviews if r.recommendation in DROPPABLE]
    if not droppable:
        return []
    dropped_wallets = set(auto_drop(conn, droppable))
    return [r for r in droppable if r.wallet in dropped_wallets]


def auto_drop(
    conn: sqlite3.Connection, reviews: list[WhaleReview]
) -> list[str]:
    """Deactivate every wallet whose recommendation is DROP or DROP_DORMANT.

    Returns the list of dropped wallets. Manual entries are exempted upstream
    in evaluate_whale (their recommendation is downgraded to 'watch').
    """
    dropped: list[str] = []
    now = int(time.time())
    for r in reviews:
        if r.recommendation not in (REC_DROP, REC_DROP_DORMANT):
            continue
        reason = f"review/{r.tier}: {r.reason[:120]}"
        cur = conn.execute(
            "UPDATE whale_watchlist SET active = 0, deactivated_at = ?, "
            "deactivated_reason = ? WHERE wallet = ? AND active = 1",
            (now, reason, r.wallet),
        )
        if cur.rowcount > 0:
            dropped.append(r.wallet)
    conn.commit()
    return dropped
