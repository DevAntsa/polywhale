"""Walk-forward validation: rolling train/test windows over historical episodes.

The gold-standard test of "does whale-following actually work out-of-sample?"

For each window pair:
  1. Training window [t0, t1]: observe each whale's PnL on episodes that
     resolved during this period. Compute their "ranking" (PnL per resolved
     episode).
  2. Test window [t1, t1+test_days]: take whales that were in the top-K by
     training-window PnL. Simulate following them: for each NEW entry they
     made in the test window, take a $stake position. Compute test-window PnL.
  3. Slide forward and repeat.

If the strategy works (sharp whales stay sharp), test PnL should consistently
be positive across windows. If we just got lucky in our 18h window, we'll see
high variance with random positive/negative test windows.

Output: PnL per window pair + consistency rate (% of test windows positive).
"""

import logging
import sqlite3
import time
from dataclasses import dataclass

from polywhale.historical_backtest import reconstruct_positions_for_wallet
from polywhale.polymarket import PolymarketClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WindowResult:
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    train_whales_top_k: list[str]    # whales selected from training
    test_episodes: int               # entries simulated in test window
    test_resolved: int               # of those, how many resolved
    test_pnl: float                  # simulated PnL with our stake
    train_total_pnl: float           # sum across top-K train PnL


@dataclass(frozen=True)
class WalkForwardSummary:
    windows: list[WindowResult]
    total_test_pnl: float
    avg_test_pnl: float
    positive_test_windows: int
    negative_test_windows: int
    consistency_pct: float           # fraction of windows that were positive


def fetch_all_episodes_chronological(
    conn: sqlite3.Connection,
    client: PolymarketClient,
    *,
    only_active_watchlist: bool = True,
) -> list:
    """Pull episodes for every active watchlist wallet, sorted by entry_ts.

    Each episode has wallet, entry_ts, exit_ts (or None), pnl_usd (or None).
    """
    sql = "SELECT wallet FROM whale_watchlist"
    if only_active_watchlist:
        sql += " WHERE active = 1"
    wallets = [r["wallet"] for r in conn.execute(sql)]
    all_eps = []
    for w in wallets:
        eps = reconstruct_positions_for_wallet(conn, client, w)
        all_eps.extend(eps)
    all_eps.sort(key=lambda e: e.entry_ts)
    return all_eps


def walk_forward(
    conn: sqlite3.Connection,
    client: PolymarketClient,
    *,
    train_days: int = 14,
    test_days: int = 7,
    top_k: int = 5,
    stake_usd: float = 40.0,
) -> WalkForwardSummary:
    """Slide rolling (train, test) windows across the episode timeline.

    train_days: length of training observation window
    test_days:  length of test simulation window
    top_k:      how many whales to select from training (by PnL)
    stake_usd:  notional per simulated copy bet

    Returns one WindowResult per slide + summary stats.
    """
    episodes = fetch_all_episodes_chronological(conn, client)
    resolved = [e for e in episodes if e.pnl_usd is not None and e.exit_ts is not None]
    if not resolved:
        return WalkForwardSummary(
            windows=[], total_test_pnl=0.0, avg_test_pnl=0.0,
            positive_test_windows=0, negative_test_windows=0, consistency_pct=0.0,
        )

    earliest = min(e.entry_ts for e in episodes)
    latest = max(e.exit_ts or e.entry_ts for e in resolved)
    train_s = train_days * 86400
    test_s = test_days * 86400
    window_step = test_s  # advance by test_days each slide

    windows: list[WindowResult] = []
    cursor = earliest
    while cursor + train_s + test_s <= latest:
        train_lo, train_hi = cursor, cursor + train_s
        test_lo, test_hi = train_hi, train_hi + test_s
        windows.append(_evaluate_window(
            resolved, train_lo, train_hi, test_lo, test_hi,
            top_k=top_k, stake_usd=stake_usd,
        ))
        cursor += window_step

    if not windows:
        return WalkForwardSummary(
            windows=[], total_test_pnl=0.0, avg_test_pnl=0.0,
            positive_test_windows=0, negative_test_windows=0, consistency_pct=0.0,
        )

    total = sum(w.test_pnl for w in windows)
    positive = sum(1 for w in windows if w.test_pnl > 0)
    negative = len(windows) - positive
    return WalkForwardSummary(
        windows=windows,
        total_test_pnl=round(total, 2),
        avg_test_pnl=round(total / len(windows), 2),
        positive_test_windows=positive,
        negative_test_windows=negative,
        consistency_pct=round(positive / len(windows) * 100, 1),
    )


def _evaluate_window(
    resolved_episodes: list,
    train_lo: int,
    train_hi: int,
    test_lo: int,
    test_hi: int,
    *,
    top_k: int,
    stake_usd: float,
) -> WindowResult:
    """Rank whales by training-window PnL, then simulate following top-K in test."""
    # Aggregate training-window PnL per wallet
    train_pnl: dict[str, float] = {}
    for e in resolved_episodes:
        if e.exit_ts is None or e.pnl_usd is None:
            continue
        if train_lo <= e.exit_ts < train_hi:
            train_pnl[e.wallet] = train_pnl.get(e.wallet, 0.0) + e.pnl_usd
    # Top K by PnL (positive only)
    ranked = sorted(
        ((w, p) for w, p in train_pnl.items() if p > 0),
        key=lambda kv: -kv[1],
    )[:top_k]
    top_wallets = [w for w, _ in ranked]
    train_total = sum(p for _, p in ranked)
    # Simulate following top_wallets in test window:
    #   for each episode entry_ts in [test_lo, test_hi) by these wallets, take stake
    test_eps = 0
    test_resolved_count = 0
    test_pnl = 0.0
    for e in resolved_episodes:
        if e.wallet not in top_wallets:
            continue
        if not (test_lo <= e.entry_ts < test_hi):
            continue
        test_eps += 1
        # Simulate at $stake: scale the whale's PnL by (stake / whale's notional)
        if e.shares <= 0 or e.entry_vwap <= 0:
            continue
        whale_notional = e.shares * e.entry_vwap
        if whale_notional <= 0:
            continue
        scale = stake_usd / whale_notional
        our_pnl = (e.pnl_usd or 0.0) * scale
        test_pnl += our_pnl
        test_resolved_count += 1
    return WindowResult(
        train_start=train_lo,
        train_end=train_hi,
        test_start=test_lo,
        test_end=test_hi,
        train_whales_top_k=top_wallets,
        test_episodes=test_eps,
        test_resolved=test_resolved_count,
        test_pnl=round(test_pnl, 2),
        train_total_pnl=round(train_total, 2),
    )


def format_summary(summary: WalkForwardSummary) -> str:
    if not summary.windows:
        return "Walk-forward: no completed window pairs (need more historical data)"
    lines = [
        f"=== Walk-Forward ({len(summary.windows)} window pairs) ===",
        f"  total test PnL          : ${summary.total_test_pnl:+,.2f}",
        f"  avg test PnL/window     : ${summary.avg_test_pnl:+,.2f}",
        f"  positive test windows   : {summary.positive_test_windows}",
        f"  negative test windows   : {summary.negative_test_windows}",
        f"  consistency             : {summary.consistency_pct}%",
        "",
        "Window-by-window:",
    ]
    for i, w in enumerate(summary.windows):
        train_iso = time.strftime("%m-%d", time.gmtime(w.train_start))
        train_end_iso = time.strftime("%m-%d", time.gmtime(w.train_end))
        test_end_iso = time.strftime("%m-%d", time.gmtime(w.test_end))
        lines.append(
            f"  {i+1:>2}. train {train_iso}->{train_end_iso} "
            f"-> test ->{test_end_iso}: "
            f"top={len(w.train_whales_top_k)} eps={w.test_episodes} "
            f"resolved={w.test_resolved} "
            f"train_pnl=${w.train_total_pnl:+,.0f} "
            f"test_pnl=${w.test_pnl:+,.2f}"
        )
    return "\n".join(lines)
