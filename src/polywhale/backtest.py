"""Replay historical whale signals into synthetic paper bets to measure edge.

Walks the `whale_signals` table since N days ago. For each signal:
  1. Assume we'd have bought the whale's outcome at the price observed when the signal fired.
  2. Look up the market via Gamma to see if it has resolved.
  3. If resolved: compute synthetic PnL (payout - entry_price) * shares.
  4. If not resolved: count as pending; PnL is null until next backtest run.

Output groups PnL by wallet (per-whale attribution) and by conviction bucket
(does the McDonald 2019 overreaction filter pay off?). Same signal data the
live bot is alerting on, just retroactively scored against gamma resolutions.

Limitation: 'current_price' on a signal is whatever the data-api reported when
we snapshotted, not the actual fill price the whale paid. Treat the PnL as
'what if we'd taken the market price at signal time', not as a precise replica.
"""

import logging
import sqlite3
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from polywhale.polymarket import PolyMarket, PolymarketClient

logger = logging.getLogger(__name__)


@dataclass
class BacktestBet:
    signal_id: int
    wallet: str
    asset_id: str
    market_slug: str
    signal_type: str
    entry_price: float
    conviction: float
    stake_usd: float
    placed_at: int
    resolved: bool = False
    won: bool | None = None
    payout: float = 0.0
    pnl: float = 0.0


@dataclass
class BacktestSummary:
    signals_total: int
    bets_placed: int
    bets_resolved: int
    bets_unresolved: int
    total_pnl: float
    by_wallet: dict = field(default_factory=dict)
    by_bucket: dict = field(default_factory=dict)


def collect_signals(
    conn: sqlite3.Connection,
    *,
    since_days: int = 30,
    signal_types: tuple[str, ...] = ("new_position", "added_size"),
    min_conviction: float = 0.0,
) -> list[sqlite3.Row]:
    """Return signals from the last `since_days` days matching the filter.

    min_conviction: drops signals whose conviction_discount is below this floor
    (use 0.5 to keep only non-overreaction signals, 1.0 to keep only undiscounted).
    """
    cutoff = int(time.time()) - since_days * 86400
    placeholders = ",".join("?" for _ in signal_types)
    sql = (
        "SELECT * FROM whale_signals "
        f"WHERE detected_at >= ? AND signal_type IN ({placeholders}) "
        "AND COALESCE(conviction_discount, 1.0) >= ? "
        "ORDER BY detected_at ASC"
    )
    params: list[object] = [cutoff, *signal_types, min_conviction]
    return conn.execute(sql, params).fetchall()


def synthesize_bets(
    rows: Iterable[sqlite3.Row],
    *,
    stake_per_signal: float = 100.0,
    weight_by_conviction: bool = True,
) -> list[BacktestBet]:
    """One bet per signal at the price recorded when the signal fired.

    If weight_by_conviction=True, the stake is multiplied by the conviction discount
    (0.5 discount → $50 stake instead of $100). This implements idea #10 — bigger
    bets behind stronger signals.
    """
    bets: list[BacktestBet] = []
    for r in rows:
        price = r["current_price"]
        if price is None or price <= 0 or price >= 1:
            continue
        conviction = (
            r["conviction_discount"] if r["conviction_discount"] is not None else 1.0
        )
        conviction_f = float(conviction)
        stake = stake_per_signal * conviction_f if weight_by_conviction else stake_per_signal
        bets.append(
            BacktestBet(
                signal_id=int(r["signal_id"]),
                wallet=r["wallet"],
                asset_id=r["asset_id"] or "",
                market_slug=r["market_slug"] or "",
                signal_type=r["signal_type"],
                entry_price=float(price),
                conviction=conviction_f,
                stake_usd=float(stake),
                placed_at=int(r["detected_at"]),
            )
        )
    return bets


def resolve_bets(
    bets: list[BacktestBet],
    client: PolymarketClient,
    *,
    market_cache: dict[str, PolyMarket | None] | None = None,
) -> list[BacktestBet]:
    """Look up each bet's market on Gamma; mark resolved bets with PnL.

    Pass `market_cache` to share gamma lookups across multiple resolve_bets calls.
    """
    cache: dict[str, PolyMarket | None] = market_cache if market_cache is not None else {}
    for bet in bets:
        if not bet.market_slug or not bet.asset_id:
            continue
        market = cache.get(bet.market_slug)
        if bet.market_slug not in cache:
            try:
                market = client.get_market(bet.market_slug)
            except Exception as exc:
                logger.warning("gamma lookup failed for %s: %s", bet.market_slug, exc)
                market = None
            cache[bet.market_slug] = market
        if not market or not market.closed or not market.outcome_prices:
            continue
        try:
            idx = market.token_ids.index(bet.asset_id)
        except ValueError:
            continue
        if idx >= len(market.outcome_prices):
            continue
        resolved_price = float(market.outcome_prices[idx])
        won = resolved_price >= 0.99
        payout = 1.0 if won else 0.0
        shares = bet.stake_usd / bet.entry_price if bet.entry_price > 0 else 0.0
        pnl = (payout - bet.entry_price) * shares
        bet.resolved = True
        bet.won = won
        bet.payout = payout
        bet.pnl = round(pnl, 4)
    return bets


def summarize(
    signals_total: int,
    bets: list[BacktestBet],
) -> BacktestSummary:
    by_wallet: dict[str, dict] = defaultdict(
        lambda: {"bets": 0, "settled": 0, "wins": 0, "losses": 0, "pnl": 0.0}
    )
    by_bucket: dict[str, dict] = defaultdict(
        lambda: {"bets": 0, "settled": 0, "wins": 0, "losses": 0, "pnl": 0.0}
    )
    resolved_count = 0
    total_pnl = 0.0
    for b in bets:
        bucket = _conviction_bucket(b.conviction)
        by_wallet[b.wallet]["bets"] += 1
        by_bucket[bucket]["bets"] += 1
        if b.resolved:
            resolved_count += 1
            total_pnl += b.pnl
            by_wallet[b.wallet]["settled"] += 1
            by_bucket[bucket]["settled"] += 1
            by_wallet[b.wallet]["pnl"] += b.pnl
            by_bucket[bucket]["pnl"] += b.pnl
            if b.won:
                by_wallet[b.wallet]["wins"] += 1
                by_bucket[bucket]["wins"] += 1
            else:
                by_wallet[b.wallet]["losses"] += 1
                by_bucket[bucket]["losses"] += 1
    return BacktestSummary(
        signals_total=signals_total,
        bets_placed=len(bets),
        bets_resolved=resolved_count,
        bets_unresolved=len(bets) - resolved_count,
        total_pnl=round(total_pnl, 2),
        by_wallet=dict(by_wallet),
        by_bucket=dict(by_bucket),
    )


def _conviction_bucket(c: float) -> str:
    if c >= 0.99:
        return "full"
    if c >= 0.75:
        return "discount-light"
    if c >= 0.55:
        return "discount-heavy"
    return "discount-floor"
