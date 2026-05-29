"""Leaderboard discovery sweep: find new whales matching our copy-trade criteria.

User-specified targets (the kind of whale we want to tail):
  - Total trades       >= 10
  - Win rate           >= 70 %
  - Total volume       >= $300,000
  - Portfolio value    >  $0  (still active, holds open positions)

Pipeline:
  1. Pull Polymarket's public leaderboard for `profit` and `volume` over the
     given window. Volume already comes from the leaderboard so the >$300K
     filter is cheap.
  2. For surviving wallets, fetch their open positions in parallel — sum
     `current_value` to get portfolio_value. Filter portfolio_value > 0.
  3. For the rest, run compute_activity_stats — gets WR and resolved-trade
     count via data-api/activity. Filter trades >= 10 AND WR >= 70%.
  4. Rank by win_rate * sqrt(volume) (skill weighted by capital deployed),
     return Candidate rows.

This is read-only — the caller decides whether to upsert into the watchlist.
"""

from __future__ import annotations

import logging
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from polywhale.polymarket import PolymarketClient
from polywhale.whale_stats import compute_activity_stats

logger = logging.getLogger(__name__)

# User-specified thresholds. Tunable but these are the defaults the user
# pointed at (the SecondWindCapital pattern: 10/82%/$536K/$891K portfolio).
MIN_TRADES = 10
MIN_WIN_RATE_PCT = 70.0
MIN_VOLUME_USD = 300_000.0
MIN_PORTFOLIO_USD = 0.01  # > 0, but with a dust floor so dead wallets fall out

# Concurrency caps — same discipline as snapshot_wallets_parallel.
PORTFOLIO_FETCH_WORKERS = 8
ACTIVITY_FETCH_WORKERS = 5


@dataclass(frozen=True)
class Candidate:
    wallet: str
    pseudonym: str | None
    profit: float
    volume: float
    portfolio_value: float
    n_resolved: int
    win_rate_pct: float
    rank_score: float


def _portfolio_value(client: PolymarketClient, wallet: str) -> float:
    try:
        positions = client.get_whale_positions(wallet, size_threshold=0.0)
    except Exception as exc:
        logger.debug("portfolio fetch failed for %s: %s", wallet[:14], exc)
        return 0.0
    return sum(float(p.current_value or 0.0) for p in positions)


def discover_candidates(
    client: PolymarketClient,
    *,
    window: str = "30d",
    leaderboard_depth: int = 500,
    min_trades: int = MIN_TRADES,
    min_win_rate_pct: float = MIN_WIN_RATE_PCT,
    min_volume_usd: float = MIN_VOLUME_USD,
    min_portfolio_usd: float = MIN_PORTFOLIO_USD,
) -> list[Candidate]:
    """Run the discovery pipeline and return ranked candidates."""
    logger.info(
        "discovery: pulling leaderboard depth=%d window=%s",
        leaderboard_depth, window,
    )
    profit_rows = client.get_leaderboard("profit", window=window)
    volume_rows = client.get_leaderboard("volume", window=window)
    vol_by_wallet = {v.wallet: v.amount for v in volume_rows}

    # Stage 1: leaderboard volume filter (cheap — already in the response).
    stage1: list[tuple[str, str | None, float, float]] = []
    for p in profit_rows[:leaderboard_depth]:
        vol = vol_by_wallet.get(p.wallet, 0.0)
        if vol >= min_volume_usd:
            stage1.append((p.wallet, p.pseudonym, p.amount, vol))
    logger.info(
        "discovery stage1: %d/%d wallets pass volume>=$%.0f",
        len(stage1), len(profit_rows[:leaderboard_depth]), min_volume_usd,
    )

    # Stage 2: portfolio value > 0 (parallel HTTP).
    portfolios: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=PORTFOLIO_FETCH_WORKERS) as ex:
        future_to_wallet = {
            ex.submit(_portfolio_value, client, w): w for w, _, _, _ in stage1
        }
        for f in as_completed(future_to_wallet):
            wallet = future_to_wallet[f]
            try:
                portfolios[wallet] = f.result()
            except Exception:
                portfolios[wallet] = 0.0
    stage2 = [
        (w, p, pr, vol, portfolios.get(w, 0.0))
        for (w, p, pr, vol) in stage1
        if portfolios.get(w, 0.0) >= min_portfolio_usd
    ]
    logger.info(
        "discovery stage2: %d/%d wallets pass portfolio>$%.2f",
        len(stage2), len(stage1), min_portfolio_usd,
    )

    # Stage 3: trades + WR via activity (parallel HTTP, slower per call).
    activity_results: dict[str, tuple[int, float | None]] = {}
    with ThreadPoolExecutor(max_workers=ACTIVITY_FETCH_WORKERS) as ex:
        future_to_wallet = {
            ex.submit(compute_activity_stats, client, w): w
            for (w, _, _, _, _) in stage2
        }
        for f in as_completed(future_to_wallet):
            wallet = future_to_wallet[f]
            try:
                stats = f.result()
                activity_results[wallet] = (
                    int(stats.unique_markets or 0),
                    stats.win_rate_pct,
                )
            except Exception:
                activity_results[wallet] = (0, None)

    candidates: list[Candidate] = []
    for w, pseudo, profit, vol, port in stage2:
        n, wr = activity_results.get(w, (0, None))
        if n < min_trades or wr is None or wr < min_win_rate_pct:
            continue
        # Skill weighted by sqrt-capital so we don't just rank by raw volume.
        rank_score = wr * math.sqrt(vol / 1000.0)
        candidates.append(
            Candidate(
                wallet=w,
                pseudonym=pseudo,
                profit=profit,
                volume=vol,
                portfolio_value=port,
                n_resolved=n,
                win_rate_pct=wr,
                rank_score=rank_score,
            )
        )
    candidates.sort(key=lambda c: c.rank_score, reverse=True)
    logger.info("discovery stage3: %d candidates met all criteria", len(candidates))
    return candidates
