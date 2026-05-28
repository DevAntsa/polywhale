"""Compute per-wallet activity stats from data-api/activity.

The lb-api only exposes profit + volume per window. To filter on win rate
and recency (which the user wants for the auto-discovery pipeline), we
have to reconstruct trade history.

Approximation used (Phase 1):
  - Group activity by conditionId (= one position per market)
  - "won" = the wallet had at least one REDEEM event on that market
  - "resolved" = the wallet has activity on that market AND we've seen them
    redeem or the market is past their last trade timestamp (proxy for closure)
  - WR ≈ won_markets / resolved_markets

This MISSES:
  - Wins from selling early at a profitable price (no REDEEM event)
  - Losses where the whale held to losing resolution (no REDEEM, no trace)

A Phase 2 version would join each market with its gamma resolution for
precise win/loss attribution. For now, approximation is fast and directional.
"""

import logging
from dataclasses import dataclass

from polywhale.polymarket import PolymarketClient

logger = logging.getLogger(__name__)

TRADE_TYPE = "TRADE"
REDEEM_TYPE = "REDEEM"


@dataclass(frozen=True)
class ActivityStats:
    wallet: str
    last_trade_at: int | None       # unix ts of newest TRADE event
    unique_markets: int             # distinct conditionIds with TRADE activity
    won_markets: int                # markets where at least one REDEEM occurred
    win_rate_pct: float | None      # won / unique (None if unique < min_samples)
    sample_size: int                # = unique_markets (clarity in output)
    pulled_events: int              # how many activity rows we examined


def compute_activity_stats(
    client: PolymarketClient,
    wallet: str,
    *,
    limit: int = 500,
    min_samples: int = 5,
) -> ActivityStats:
    """Fetch the wallet's recent activity and reduce to WR + last_trade.

    `min_samples`: WR is None until the wallet has this many unique markets.
    Returns empty/None stats on any error so callers can default gracefully.
    """
    try:
        events = client.get_activity(wallet, limit=limit)
    except Exception as exc:
        logger.warning("get_activity failed for %s: %s", wallet[:14], exc)
        return ActivityStats(
            wallet=wallet, last_trade_at=None, unique_markets=0,
            won_markets=0, win_rate_pct=None, sample_size=0, pulled_events=0,
        )
    last_trade_at: int | None = None
    trade_markets: set[str] = set()
    redeem_markets: set[str] = set()
    for ev in events:
        t = ev.get("type")
        cond = ev.get("conditionId")
        if not cond:
            continue
        if t == TRADE_TYPE:
            trade_markets.add(cond)
            ts = ev.get("timestamp")
            if ts is not None:
                ts_int = int(ts)
                if last_trade_at is None or ts_int > last_trade_at:
                    last_trade_at = ts_int
        elif t == REDEEM_TYPE:
            redeem_markets.add(cond)
    unique = len(trade_markets)
    won = len(redeem_markets & trade_markets)
    if unique >= min_samples:
        wr_pct = round((won / unique) * 100.0, 1)
    else:
        wr_pct = None
    return ActivityStats(
        wallet=wallet,
        last_trade_at=last_trade_at,
        unique_markets=unique,
        won_markets=won,
        win_rate_pct=wr_pct,
        sample_size=unique,
        pulled_events=len(events),
    )


def passes_activity_filter(
    stats: ActivityStats,
    *,
    max_dormant_days: int = 14,
    min_wr_pct: float = 60.0,
    min_sample: int = 20,
    now_ts: int | None = None,
) -> tuple[bool, str]:
    """Decide if a candidate's activity stats qualify them for the watchlist.

    Returns (passes, reason). 'reason' is the failing criterion or 'ok'.
    """
    import time as _time
    now = now_ts if now_ts is not None else int(_time.time())
    if stats.last_trade_at is None:
        return False, "no_trade_history"
    age_days = (now - stats.last_trade_at) / 86400.0
    if age_days > max_dormant_days:
        return False, f"dormant_{age_days:.1f}d"
    if stats.sample_size < min_sample:
        return False, f"sample_too_small_{stats.sample_size}"
    if stats.win_rate_pct is None or stats.win_rate_pct < min_wr_pct:
        return False, f"wr_below_{min_wr_pct}_(actual {stats.win_rate_pct})"
    return True, "ok"
