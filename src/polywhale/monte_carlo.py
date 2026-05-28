"""Bootstrap Monte Carlo simulation over our historical paper-bet PnL distribution.

The core question: given the 86 closed copy-bets we've observed, is the +$56
realized PnL a lucky one-shot or representative of edge? We resample with
replacement N times to build a distribution of possible future weekly outcomes.

Two modes:
  - Aggregated: pool all closed bets across whales, resample globally.
  - Per-whale:  resample each whale's own trade history independently and
                sum, preserving the rate-of-trading and PnL-distribution
                characteristics of each whale separately.

Caveats:
  - Assumes future trade distribution looks like the past. Whale dies or market
    regime changes → MC overestimates.
  - Assumes trade outcomes are i.i.d. They aren't (clustered by market resolution),
    but bootstrap is still a useful first-order estimator.
  - Bankroll cap is not modeled here. Real future weeks will reject some bets
    when cap engages; MC gives a "what could the bot make if cap was infinite"
    upper bound on PnL trajectory.
"""

import random
import sqlite3
import statistics
from dataclasses import dataclass, field

WHALE_COPY_SOURCE = "whale_copy"


@dataclass(frozen=True)
class MonteCarloResult:
    mode: str                     # "aggregated" or "per-whale"
    samples: int                  # how many bootstrap simulations
    horizon_days: int
    trades_per_sample: int        # avg trades simulated per sample
    historical_trades: int        # how many actual trades we sampled from
    historical_mean: float
    historical_stdev: float
    # Distribution of total PnL over horizon
    mean_pnl: float
    median_pnl: float
    p5: float
    p25: float
    p75: float
    p95: float
    prob_positive: float          # fraction of samples that net positive
    # Max drawdown distribution across samples
    median_drawdown: float
    p95_drawdown: float           # 95th-pct (= bad-scenario) drawdown
    per_whale_breakdown: dict = field(default_factory=dict)


def fetch_pnls(
    conn: sqlite3.Connection,
    *,
    source: str = WHALE_COPY_SOURCE,
    wallet: str | None = None,
) -> list[float]:
    """Pull closed PnLs from poly_paper_bets. Optionally filtered to one whale."""
    if wallet:
        rows = conn.execute(
            """
            SELECT pb.pnl_usd FROM poly_paper_bets pb
            JOIN whale_signals ws ON pb.source_ref_id = ws.signal_id
            WHERE pb.source = ? AND pb.settled_at IS NOT NULL
              AND ws.wallet = ? AND pb.pnl_usd IS NOT NULL
            """,
            (source, wallet),
        )
    else:
        rows = conn.execute(
            "SELECT pnl_usd FROM poly_paper_bets "
            "WHERE source = ? AND settled_at IS NOT NULL AND pnl_usd IS NOT NULL",
            (source,),
        )
    return [float(r[0]) for r in rows]


def estimate_trades_per_day(
    conn: sqlite3.Connection,
    *,
    source: str = WHALE_COPY_SOURCE,
    wallet: str | None = None,
) -> float:
    """Closed-bet throughput observed per day. Used to pick `trades_per_sample`."""
    if wallet:
        row = conn.execute(
            """
            SELECT MIN(pb.placed_at) AS lo, MAX(pb.settled_at) AS hi, COUNT(*) AS n
            FROM poly_paper_bets pb
            JOIN whale_signals ws ON pb.source_ref_id = ws.signal_id
            WHERE pb.source = ? AND pb.settled_at IS NOT NULL AND ws.wallet = ?
            """,
            (source, wallet),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT MIN(placed_at) AS lo, MAX(settled_at) AS hi, COUNT(*) AS n "
            "FROM poly_paper_bets WHERE source = ? AND settled_at IS NOT NULL",
            (source,),
        ).fetchone()
    if not row or not row["n"] or row["lo"] is None or row["hi"] is None:
        return 0.0
    span_days = max((row["hi"] - row["lo"]) / 86400.0, 1 / 24)
    return row["n"] / span_days


def max_drawdown(sequence: list[float]) -> float:
    """Peak-to-trough drawdown for a cumulative-sum PnL trajectory."""
    cum = 0.0
    peak = 0.0
    worst = 0.0
    for p in sequence:
        cum += p
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > worst:
            worst = dd
    return worst


def _percentile(sorted_arr: list[float], pct: float) -> float:
    if not sorted_arr:
        return 0.0
    idx = max(0, min(int(len(sorted_arr) * pct / 100.0), len(sorted_arr) - 1))
    return sorted_arr[idx]


def simulate_aggregated(
    conn: sqlite3.Connection,
    *,
    horizon_days: int = 7,
    samples: int = 10_000,
    seed: int | None = None,
) -> MonteCarloResult:
    """Pool all closed bets together; bootstrap-resample to estimate future PnL."""
    if seed is not None:
        random.seed(seed)
    pnls = fetch_pnls(conn)
    if not pnls:
        raise ValueError("no closed whale_copy bets to sample from")
    rate = estimate_trades_per_day(conn)
    trades_per_sample = max(round(rate * horizon_days), 1)
    totals: list[float] = []
    drawdowns: list[float] = []
    for _ in range(samples):
        draws = random.choices(pnls, k=trades_per_sample)
        totals.append(sum(draws))
        drawdowns.append(max_drawdown(draws))
    totals.sort()
    drawdowns.sort()
    return MonteCarloResult(
        mode="aggregated",
        samples=samples,
        horizon_days=horizon_days,
        trades_per_sample=trades_per_sample,
        historical_trades=len(pnls),
        historical_mean=round(statistics.mean(pnls), 4),
        historical_stdev=round(statistics.stdev(pnls), 4) if len(pnls) > 1 else 0.0,
        mean_pnl=round(statistics.mean(totals), 2),
        median_pnl=round(_percentile(totals, 50), 2),
        p5=round(_percentile(totals, 5), 2),
        p25=round(_percentile(totals, 25), 2),
        p75=round(_percentile(totals, 75), 2),
        p95=round(_percentile(totals, 95), 2),
        prob_positive=round(sum(1 for t in totals if t > 0) / len(totals), 4),
        median_drawdown=round(_percentile(drawdowns, 50), 2),
        p95_drawdown=round(_percentile(drawdowns, 95), 2),
    )


def simulate_per_whale(
    conn: sqlite3.Connection,
    *,
    horizon_days: int = 7,
    samples: int = 10_000,
    min_trades_per_whale: int = 1,
    seed: int | None = None,
) -> MonteCarloResult:
    """Resample each whale's trades independently, then sum per simulation.

    Preserves the per-whale rate and per-whale PnL distribution. A whale that
    only trades 1x/day contributes proportionally less than one trading 20x/day.
    """
    if seed is not None:
        random.seed(seed)
    whale_meta = list(
        conn.execute(
            """
            SELECT ws.wallet, COUNT(*) AS n,
                   MIN(pb.placed_at) AS lo, MAX(pb.settled_at) AS hi
            FROM poly_paper_bets pb
            JOIN whale_signals ws ON pb.source_ref_id = ws.signal_id
            WHERE pb.source = 'whale_copy' AND pb.settled_at IS NOT NULL
            GROUP BY ws.wallet
            HAVING COUNT(*) >= ?
            """,
            (min_trades_per_whale,),
        )
    )
    if not whale_meta:
        raise ValueError("no whales with enough closed trades to sample")

    whale_setup: list[tuple[str, list[float], int]] = []
    all_pnls: list[float] = []
    for w in whale_meta:
        wallet = w["wallet"]
        pnls = fetch_pnls(conn, wallet=wallet)
        if not pnls:
            continue
        span_days = max((w["hi"] - w["lo"]) / 86400.0, 1 / 24)
        rate = w["n"] / span_days
        trades_in_horizon = max(round(rate * horizon_days), 0)
        if trades_in_horizon == 0:
            continue
        whale_setup.append((wallet, pnls, trades_in_horizon))
        all_pnls.extend(pnls)

    totals: list[float] = []
    drawdowns: list[float] = []
    per_whale_mean_pnl: dict[str, float] = {
        wallet: round(statistics.mean(pnls) * tih, 2)
        for wallet, pnls, tih in whale_setup
    }
    total_trades_per_sample = sum(tih for _, _, tih in whale_setup)
    for _ in range(samples):
        sequence: list[float] = []
        for _, pnls, tih in whale_setup:
            sequence.extend(random.choices(pnls, k=tih))
        random.shuffle(sequence)  # interleave so drawdown reflects mixed-whale runs
        totals.append(sum(sequence))
        drawdowns.append(max_drawdown(sequence))
    totals.sort()
    drawdowns.sort()
    return MonteCarloResult(
        mode="per-whale",
        samples=samples,
        horizon_days=horizon_days,
        trades_per_sample=total_trades_per_sample,
        historical_trades=len(all_pnls),
        historical_mean=round(statistics.mean(all_pnls), 4) if all_pnls else 0.0,
        historical_stdev=round(statistics.stdev(all_pnls), 4) if len(all_pnls) > 1 else 0.0,
        mean_pnl=round(statistics.mean(totals), 2),
        median_pnl=round(_percentile(totals, 50), 2),
        p5=round(_percentile(totals, 5), 2),
        p25=round(_percentile(totals, 25), 2),
        p75=round(_percentile(totals, 75), 2),
        p95=round(_percentile(totals, 95), 2),
        prob_positive=round(sum(1 for t in totals if t > 0) / len(totals), 4),
        median_drawdown=round(_percentile(drawdowns, 50), 2),
        p95_drawdown=round(_percentile(drawdowns, 95), 2),
        per_whale_breakdown=per_whale_mean_pnl,
    )
