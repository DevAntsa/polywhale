"""Maker-side routing — shadow mode for paper trading, real-money ready.

The thesis (Cycle 5 research, 2026-05-29): Polymarket pays a 25% maker rebate
on Sports/Politics/etc. (20% Crypto, 0% Geopolitics). A maker fill captures
the rebate; a taker fill pays the full peak fee. Round-trip friction goes
from 1.5% (both taker) to ~1.125% (both maker) on Sports.

In paper mode we don't actually place orders — we just record what real-money
execution would have looked like under a maker-first strategy with taker
fallback. The shadow observer runs after each `place_copy_bet` and
`close_copy_bet`, simulates the route, and writes the result onto the bet row.

Decision logic:
  1. If the signal is time-critical (news-driven, big size jump, hot price
     movement), route taker for speed.
  2. Otherwise route maker first: limit order at the whale's signal price.
  3. Simulate fill: replay polymarket_books snapshots between signal_ts and
     signal_ts+max_wait_s. If the opposite side of the book ever touched
     our limit, count it filled. Otherwise fall back to taker at the then-
     current best.

Conservative simulation: a BUY-YES maker bid at $X is considered filled only
if best_ask <= X in a subsequent snapshot — meaning a real seller would have
needed to cross our bid. Same logic mirrored for SELL exits.

Real-money mode (future): swap the simulation for py-clob-client order
placement. The decision and accounting layers stay identical.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from polywhale.whale_sizing import (
    category_from_slug,
    fee_for_category,
    maker_rebate_for_category,
)

logger = logging.getLogger(__name__)

# How long we'll rest a maker order before falling back to taker.
DEFAULT_MAKER_WAIT_S = 300
TIME_CRITICAL_MAKER_WAIT_S = 30

# A signal is "time-critical" if the position size jumped > this fraction
# relative to the prior captured size — suggests news or sharp conviction
# burst where waiting on a limit fill costs us the edge.
TIME_CRITICAL_SIZE_JUMP = 1.0  # 100% size jump in one snapshot

ROUTE_MAKER = "maker"
ROUTE_TAKER = "taker"
ROUTE_MAKER_FALLBACK = "maker_fallback_taker"


@dataclass(frozen=True)
class RouteDecision:
    route: str            # ROUTE_MAKER | ROUTE_TAKER (decision time; outcome may differ)
    limit_price: float    # what we'd post (or pay if taker)
    max_wait_s: int
    reason: str


@dataclass(frozen=True)
class FillResult:
    route: str            # actual outcome (ROUTE_MAKER | ROUTE_TAKER | ROUTE_MAKER_FALLBACK)
    fill_price: float
    fee_usd: float        # signed: negative = fee paid, positive = rebate captured
    reason: str


def _signal_is_time_critical(signal_row: sqlite3.Row) -> bool:
    """Heuristic: very large relative size jump = news-driven, route taker."""
    old = signal_row["old_size"]
    new = signal_row["new_size"]
    if old is None or new is None:
        return False
    try:
        old_f = float(old)
        new_f = float(new)
    except (TypeError, ValueError):
        return False
    if old_f <= 0:
        return new_f > 50_000  # fresh position with massive size
    return (new_f - old_f) / old_f >= TIME_CRITICAL_SIZE_JUMP


def decide_entry_route(
    signal_row: sqlite3.Row,
    current_book_price: float,
) -> RouteDecision:
    """Decide how to enter on this whale signal."""
    if _signal_is_time_critical(signal_row):
        return RouteDecision(
            route=ROUTE_TAKER,
            limit_price=current_book_price,
            max_wait_s=0,
            reason="time-critical size jump",
        )
    return RouteDecision(
        route=ROUTE_MAKER,
        limit_price=current_book_price,
        max_wait_s=DEFAULT_MAKER_WAIT_S,
        reason="default maker-first",
    )


def decide_exit_route(
    signal_row: sqlite3.Row,
    current_book_price: float,
) -> RouteDecision:
    """Exit routing mirrors entry. Whale exits on news → taker for us too."""
    if _signal_is_time_critical(signal_row):
        return RouteDecision(
            route=ROUTE_TAKER,
            limit_price=current_book_price,
            max_wait_s=0,
            reason="time-critical whale exit",
        )
    return RouteDecision(
        route=ROUTE_MAKER,
        limit_price=current_book_price,
        max_wait_s=DEFAULT_MAKER_WAIT_S,
        reason="default maker-first exit",
    )


def _fetch_book_progression(
    conn: sqlite3.Connection,
    token_id: str,
    from_ts: int,
    to_ts: int,
) -> list[sqlite3.Row]:
    return list(conn.execute(
        """
        SELECT captured_at, best_bid, best_ask
        FROM polymarket_books
        WHERE token_id = ?
          AND captured_at >= ?
          AND captured_at <= ?
        ORDER BY captured_at ASC
        """,
        (token_id, from_ts, to_ts),
    ))


def simulate_buy_yes_fill(
    conn: sqlite3.Connection,
    *,
    token_id: str,
    stake_usd: float,
    signal_ts: int,
    decision: RouteDecision,
    category: str,
) -> FillResult:
    """Simulate filling a BUY-YES order under the given route decision.

    Maker fill rule (conservative): we get filled if best_ask drops to or
    below our limit at any snapshot within max_wait_s. Otherwise fall back
    to taker at the snapshot-then-current best_ask.
    """
    peak_fee = fee_for_category(category)
    rebate_pct = maker_rebate_for_category(category)

    if decision.route == ROUTE_TAKER:
        fee_usd = -stake_usd * peak_fee
        return FillResult(
            route=ROUTE_TAKER,
            fill_price=decision.limit_price,
            fee_usd=fee_usd,
            reason="immediate taker",
        )

    # Maker attempt — replay book forward.
    snaps = _fetch_book_progression(
        conn, token_id, signal_ts, signal_ts + decision.max_wait_s,
    )
    for snap in snaps:
        best_ask = snap["best_ask"]
        if best_ask is None:
            continue
        if best_ask <= decision.limit_price:
            rebate_usd = stake_usd * peak_fee * rebate_pct
            return FillResult(
                route=ROUTE_MAKER,
                fill_price=decision.limit_price,
                fee_usd=+rebate_usd,
                reason=f"maker filled when best_ask={best_ask:.4f}",
            )

    # Fallback to taker at the closing best_ask in window, else original price.
    fallback_price = decision.limit_price
    for snap in reversed(snaps):
        if snap["best_ask"] is not None:
            fallback_price = float(snap["best_ask"])
            break
    fee_usd = -stake_usd * peak_fee
    return FillResult(
        route=ROUTE_MAKER_FALLBACK,
        fill_price=fallback_price,
        fee_usd=fee_usd,
        reason="maker didn't fill in window, fell back to taker",
    )


def simulate_sell_yes_fill(
    conn: sqlite3.Connection,
    *,
    token_id: str,
    proceeds_usd: float,
    signal_ts: int,
    decision: RouteDecision,
    category: str,
) -> FillResult:
    """Simulate SELL-YES (closing a long) under the given route decision.

    Maker SELL fill rule: we get filled if best_bid rises to or above our
    limit ask in some snapshot — meaning a buyer would have crossed our offer.
    """
    peak_fee = fee_for_category(category)
    rebate_pct = maker_rebate_for_category(category)

    if decision.route == ROUTE_TAKER:
        fee_usd = -proceeds_usd * peak_fee
        return FillResult(
            route=ROUTE_TAKER,
            fill_price=decision.limit_price,
            fee_usd=fee_usd,
            reason="immediate taker exit",
        )

    snaps = _fetch_book_progression(
        conn, token_id, signal_ts, signal_ts + decision.max_wait_s,
    )
    for snap in snaps:
        best_bid = snap["best_bid"]
        if best_bid is None:
            continue
        if best_bid >= decision.limit_price:
            rebate_usd = proceeds_usd * peak_fee * rebate_pct
            return FillResult(
                route=ROUTE_MAKER,
                fill_price=decision.limit_price,
                fee_usd=+rebate_usd,
                reason=f"maker exit filled when best_bid={best_bid:.4f}",
            )

    fallback_price = decision.limit_price
    for snap in reversed(snaps):
        if snap["best_bid"] is not None:
            fallback_price = float(snap["best_bid"])
            break
    fee_usd = -proceeds_usd * peak_fee
    return FillResult(
        route=ROUTE_MAKER_FALLBACK,
        fill_price=fallback_price,
        fee_usd=fee_usd,
        reason="maker exit didn't fill, fallback taker",
    )


def shadow_observe_entry(
    conn: sqlite3.Connection,
    *,
    bet_id: int,
    signal_row: sqlite3.Row,
    paper_entry_price: float,
    stake_usd: float,
    market_slug: str,
) -> FillResult:
    """Compute and persist what real-money entry routing would have done."""
    category = category_from_slug(market_slug)
    decision = decide_entry_route(signal_row, paper_entry_price)
    signal_ts = signal_row["latest_captured_at"] or signal_row["detected_at"]
    fill = simulate_buy_yes_fill(
        conn,
        token_id=signal_row["asset_id"],
        stake_usd=stake_usd,
        signal_ts=int(signal_ts),
        decision=decision,
        category=category,
    )
    conn.execute(
        """
        UPDATE poly_paper_bets
        SET entry_route = ?, entry_price_routed = ?, entry_fee_usd = ?
        WHERE bet_id = ?
        """,
        (fill.route, fill.fill_price, fill.fee_usd, bet_id),
    )
    conn.commit()
    logger.info(
        "maker shadow entry bet=%d route=%s fill=%.4f fee=%+.4f (%s)",
        bet_id, fill.route, fill.fill_price, fill.fee_usd, fill.reason,
    )
    return fill


def shadow_observe_exit(
    conn: sqlite3.Connection,
    *,
    bet_id: int,
    exit_signal_row: sqlite3.Row,
    paper_exit_price: float,
    shares: float,
    market_slug: str,
) -> FillResult:
    """Compute and persist what real-money exit routing would have done."""
    category = category_from_slug(market_slug)
    decision = decide_exit_route(exit_signal_row, paper_exit_price)
    signal_ts = (
        exit_signal_row["latest_captured_at"] or exit_signal_row["detected_at"]
    )
    proceeds_usd = shares * paper_exit_price
    fill = simulate_sell_yes_fill(
        conn,
        token_id=exit_signal_row["asset_id"],
        proceeds_usd=proceeds_usd,
        signal_ts=int(signal_ts),
        decision=decision,
        category=category,
    )
    # Compute and persist routed PnL as well.
    bet = conn.execute(
        "SELECT size_shares, entry_price_routed, entry_fee_usd "
        "FROM poly_paper_bets WHERE bet_id = ?",
        (bet_id,),
    ).fetchone()
    net_pnl_routed = None
    if bet is not None and bet["entry_price_routed"] is not None:
        gross = (fill.fill_price - bet["entry_price_routed"]) * bet["size_shares"]
        net_pnl_routed = gross + (bet["entry_fee_usd"] or 0.0) + fill.fee_usd
    conn.execute(
        """
        UPDATE poly_paper_bets
        SET exit_route = ?, exit_price_routed = ?, exit_fee_usd = ?,
            net_pnl_routed_usd = ?
        WHERE bet_id = ?
        """,
        (fill.route, fill.fill_price, fill.fee_usd, net_pnl_routed, bet_id),
    )
    conn.commit()
    logger.info(
        "maker shadow exit bet=%d route=%s fill=%.4f fee=%+.4f net_routed=%s (%s)",
        bet_id, fill.route, fill.fill_price, fill.fee_usd,
        f"{net_pnl_routed:+.4f}" if net_pnl_routed is not None else "—",
        fill.reason,
    )
    return fill


def routing_report(conn: sqlite3.Connection) -> dict:
    """Aggregate routing stats: maker share, fee captured vs paid, PnL delta."""
    entries = conn.execute(
        """
        SELECT
          SUM(CASE WHEN entry_route = ? THEN 1 ELSE 0 END) AS maker,
          SUM(CASE WHEN entry_route = ? THEN 1 ELSE 0 END) AS taker,
          SUM(CASE WHEN entry_route = ? THEN 1 ELSE 0 END) AS fallback,
          SUM(COALESCE(entry_fee_usd, 0)) AS entry_fee_total,
          COUNT(entry_route) AS n_routed
        FROM poly_paper_bets
        WHERE entry_route IS NOT NULL
        """,
        (ROUTE_MAKER, ROUTE_TAKER, ROUTE_MAKER_FALLBACK),
    ).fetchone()
    exits = conn.execute(
        """
        SELECT
          SUM(CASE WHEN exit_route = ? THEN 1 ELSE 0 END) AS maker,
          SUM(CASE WHEN exit_route = ? THEN 1 ELSE 0 END) AS taker,
          SUM(CASE WHEN exit_route = ? THEN 1 ELSE 0 END) AS fallback,
          SUM(COALESCE(exit_fee_usd, 0)) AS exit_fee_total,
          COUNT(exit_route) AS n_routed
        FROM poly_paper_bets
        WHERE exit_route IS NOT NULL
        """,
        (ROUTE_MAKER, ROUTE_TAKER, ROUTE_MAKER_FALLBACK),
    ).fetchone()
    pnl_delta = conn.execute(
        """
        SELECT
          SUM(pnl_usd) AS paper_pnl_sum,
          SUM(net_pnl_routed_usd) AS routed_pnl_sum,
          COUNT(net_pnl_routed_usd) AS n_compared
        FROM poly_paper_bets
        WHERE net_pnl_routed_usd IS NOT NULL
          AND pnl_usd IS NOT NULL
        """,
    ).fetchone()
    return {
        "entry_maker": entries["maker"] or 0,
        "entry_taker": entries["taker"] or 0,
        "entry_fallback": entries["fallback"] or 0,
        "entry_n": entries["n_routed"] or 0,
        "entry_fee_total": entries["entry_fee_total"] or 0.0,
        "exit_maker": exits["maker"] or 0,
        "exit_taker": exits["taker"] or 0,
        "exit_fallback": exits["fallback"] or 0,
        "exit_n": exits["n_routed"] or 0,
        "exit_fee_total": exits["exit_fee_total"] or 0.0,
        "paper_pnl_sum": pnl_delta["paper_pnl_sum"] or 0.0,
        "routed_pnl_sum": pnl_delta["routed_pnl_sum"] or 0.0,
        "n_compared": pnl_delta["n_compared"] or 0,
    }
