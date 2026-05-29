"""1/4 Kelly fractional sizing per whale, with sample-size shrinkage + safety caps.

Replaces the flat $40 per-signal stake with variance-adjusted per-whale stakes.

Formula (Thorp 1969 continuous Kelly):
  f*_i = max(0, (mu__i - fees) / var_i)
  stake_i = bankroll x min(KELLY_FRACTION x f*_i x shrinkage(n_i), CAP_PER_BET)

Where:
  mu__i      = observed mean PnL per $1 staked for whale i
  var_i     = variance of PnL per $1 staked
  fees     = round-trip friction (default 0.015 = 1.5% Polymarket blended)
  n_i      = number of closed copy-trades for whale i
  shrinkage(n) = 0   if n < SHRINK_LOW
                 (n - SHRINK_LOW) / (SHRINK_HIGH - SHRINK_LOW)   if SHRINK_LOW ≤ n < SHRINK_HIGH
                 1   if n ≥ SHRINK_HIGH

Drop rule: if mu__i - 2sigma_i/sqrtn_i ≤ -fees, return 0 (error bar crosses friction floor).

Sources (in memory):
- reference_polywhale_research_2026-05-28.md
- Kelly (1956), Thorp (1969), MacLean/Thorp/Ziemba (2010) on fractional Kelly
- Buchdahl "Squares & Sharps" ch. 9 (1/4 Kelly for sports + parameter uncertainty)
- Chopra & Ziemba (1993) on Kelly brittleness to mu_ mis-estimate
"""

import logging
import math
import sqlite3
import statistics
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Tunable constants (frozen after calibration)
KELLY_FRACTION = 0.25                # 1/4 Kelly per literature consensus
DEFAULT_FEES_ROUND_TRIP = 0.015      # Polymarket blended sports+politics
EXPLORE_STAKE_PCT = 0.015            # 1.5% bankroll for unproven whales (paper-tune)
SHRINK_LOW = 10                       # n < this → pure exploration
SHRINK_HIGH = 30                      # n ≥ this → full Kelly
CAP_PER_BET = 0.05                   # max 5% bankroll per single bet (paper-tune)
MAX_OPEN_POSITIONS = 25
MAX_PORTFOLIO_DEPLOY_PCT = 0.50       # 50% of bankroll deployed at once (paper-tune)
MAX_CATEGORY_DEPLOY_PCT = 0.25        # 25% of bankroll per category


@dataclass(frozen=True)
class SizingResult:
    stake_usd: float
    fraction: float          # of bankroll
    reason: str              # explanation
    kelly_raw: float | None  # unshrunk Kelly fraction (for diagnostics)
    sample_size: int
    skipped: bool = False    # True if portfolio guards rejected it


def whale_pnl_stats(
    conn: sqlite3.Connection,
    wallet: str,
    *,
    base_stake_usd: float = 40.0,
) -> tuple[float, float, int]:
    """Return (mean_pnl_per_dollar, variance_pnl_per_dollar, n_trades) for a wallet.

    Normalizes per-dollar so the Kelly math is bankroll-independent. The
    base_stake_usd argument should match what was actually staked when our
    historical PnL was recorded (default $40 = the current flat-stake era).
    """
    rows = list(
        conn.execute(
            """
            SELECT pb.pnl_usd, pb.cost_usd
            FROM poly_paper_bets pb
            JOIN whale_signals ws ON pb.source_ref_id = ws.signal_id
            WHERE pb.source = 'whale_copy' AND pb.settled_at IS NOT NULL
              AND ws.wallet = ? AND pb.pnl_usd IS NOT NULL
            """,
            (wallet.lower(),),
        )
    )
    if not rows:
        return 0.0, 0.0, 0
    per_dollar = []
    for r in rows:
        cost = float(r["cost_usd"] or base_stake_usd)
        if cost <= 0:
            continue
        per_dollar.append(float(r["pnl_usd"]) / cost)
    if not per_dollar:
        return 0.0, 0.0, 0
    mu = statistics.mean(per_dollar)
    if len(per_dollar) > 1:
        sigma2 = statistics.variance(per_dollar)
    else:
        sigma2 = 0.0
    return mu, sigma2, len(per_dollar)


def compute_kelly_stake(
    conn: sqlite3.Connection,
    wallet: str,
    *,
    bankroll_usd: float,
    fees_rt: float = DEFAULT_FEES_ROUND_TRIP,
    conviction_multiplier: float = 1.0,
) -> SizingResult:
    """Compute the recommended stake for one signal from `wallet`."""
    mu, sigma2, n = whale_pnl_stats(conn, wallet)

    # Pure exploration phase — too little data to size on Kelly
    if n < SHRINK_LOW:
        stake = bankroll_usd * EXPLORE_STAKE_PCT * conviction_multiplier
        stake = min(stake, bankroll_usd * CAP_PER_BET)
        return SizingResult(
            stake_usd=round(stake, 2),
            fraction=stake / bankroll_usd,
            reason=f"exploration (n={n} < {SHRINK_LOW})",
            kelly_raw=None,
            sample_size=n,
        )

    # Drop rule: error bar crosses friction floor
    if sigma2 > 0:
        sigma = math.sqrt(sigma2)
        se = sigma / math.sqrt(n) if n > 0 else float("inf")
        if mu - 2 * se <= -fees_rt:
            return SizingResult(
                stake_usd=0.0,
                fraction=0.0,
                reason=f"drop: mu_-2sigma/sqrtn ({mu - 2 * se:+.4f}) ≤ -fees ({-fees_rt:.4f})",
                kelly_raw=None,
                sample_size=n,
                skipped=True,
            )

    # Kelly with friction
    mu_net = mu - fees_rt
    if mu_net <= 0:
        return SizingResult(
            stake_usd=0.0,
            fraction=0.0,
            reason=f"drop: mu_ net of fees non-positive ({mu_net:+.4f})",
            kelly_raw=None,
            sample_size=n,
            skipped=True,
        )

    if sigma2 <= 1e-9:
        # All trades identical (e.g. all $0 PnL closes). Skip — no
        # meaningful Kelly when variance is zero.
        return SizingResult(
            stake_usd=0.0,
            fraction=0.0,
            reason=f"drop: variance ≈ 0 (mu={mu:+.4f}, n={n})",
            kelly_raw=None,
            sample_size=n,
            skipped=True,
        )

    kelly_raw = mu_net / sigma2
    f_quarter = KELLY_FRACTION * kelly_raw

    # Sample-size shrinkage (linear between SHRINK_LOW and SHRINK_HIGH)
    if n < SHRINK_HIGH:
        shrink = (n - SHRINK_LOW) / (SHRINK_HIGH - SHRINK_LOW)
        f_target = shrink * f_quarter + (1 - shrink) * EXPLORE_STAKE_PCT
    else:
        f_target = f_quarter

    # Conviction discount (existing McDonald 2019 overreaction filter)
    f_target *= conviction_multiplier

    # Per-bet hard cap
    f_target = min(max(0.0, f_target), CAP_PER_BET)

    stake = bankroll_usd * f_target
    return SizingResult(
        stake_usd=round(stake, 2),
        fraction=f_target,
        reason=(
            f"kelly n={n} mu={mu:+.4f} var={sigma2:.4f} "
            f"kelly={kelly_raw:.3f} shrunk={f_target:.4f}"
        ),
        kelly_raw=round(kelly_raw, 4),
        sample_size=n,
    )


def check_portfolio_guards(
    conn: sqlite3.Connection,
    proposed_stake: float,
    *,
    bankroll_usd: float,
    market_slug: str | None,
    outcome: str | None,
    category_proxy: str | None = None,
) -> tuple[bool, str]:
    """Apply portfolio-level guards: max positions, deployment cap, same-market dedup.

    Category cap is not implemented here yet — needs a `category` field on bets.
    Returns (allowed, reason).
    """
    open_row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd), 0) AS deployed "
        "FROM poly_paper_bets WHERE source = 'whale_copy' AND settled_at IS NULL"
    ).fetchone()
    open_n = int(open_row["n"] or 0)
    deployed = float(open_row["deployed"] or 0)

    if open_n >= MAX_OPEN_POSITIONS:
        return False, f"max_open_positions ({open_n} >= {MAX_OPEN_POSITIONS})"

    if deployed + proposed_stake > bankroll_usd * MAX_PORTFOLIO_DEPLOY_PCT:
        return False, (
            f"portfolio_deploy_cap "
            f"(deployed ${deployed:.2f} + ${proposed_stake:.2f} > "
            f"{MAX_PORTFOLIO_DEPLOY_PCT * 100:.0f}% of ${bankroll_usd:.0f})"
        )

    # Same-(market, side) dedup
    if market_slug and outcome:
        dupe = conn.execute(
            "SELECT 1 FROM poly_paper_bets WHERE source = 'whale_copy' "
            "AND settled_at IS NULL AND market_slug = ? AND outcome_title = ?",
            (market_slug, outcome),
        ).fetchone()
        if dupe:
            return False, f"dedup_same_market ({market_slug[:30]} / {outcome})"

    return True, "ok"
