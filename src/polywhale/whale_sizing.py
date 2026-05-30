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

# Per-category Polymarket fees (Cycle 5 research, 2026-05-29).
# Format: peak one-way taker fee at p=0.50. Round-trip is approximately 2x for
# taker-taker execution. The fee scales by p(1-p) so it's smaller off the 50/50
# line. Geopolitics is fee-free as of March 30, 2026 rollout.
# Source: help.polymarket.com/articles/13364478, docs.polymarket.com/fees.
FEES_BY_CATEGORY = {
    "sports":      0.0075,
    "politics":    0.01,
    "finance":     0.01,
    "tech":        0.01,
    "mentions":    0.01,
    "economics":   0.0125,
    "culture":     0.0125,
    "weather":     0.0125,
    "crypto":      0.018,
    "geopolitics": 0.0,
    # Default / unknown category — use Sports as conservative baseline.
    "default":     0.0075,
}

# Maker rebate as a fraction of taker fees, paid daily by Polymarket.
# A maker on Sports captures 25% of the taker fee back, so effective maker-side
# friction is approximately fee * (1 - rebate) ≈ 0 (depending on order matching).
MAKER_REBATE_BY_CATEGORY = {
    "sports":      0.25,
    "politics":    0.25,
    "finance":     0.25,
    "tech":        0.25,
    "mentions":    0.25,
    "economics":   0.25,
    "culture":     0.25,
    "weather":     0.25,
    "crypto":      0.20,
    "geopolitics": 0.0,
    "default":     0.25,
}
MAX_CATEGORY_DEPLOY_PCT = 0.25        # 25% of bankroll per category


@dataclass(frozen=True)
class SizingResult:
    stake_usd: float
    fraction: float          # of bankroll
    reason: str              # explanation
    kelly_raw: float | None  # unshrunk Kelly fraction (for diagnostics)
    sample_size: int
    skipped: bool = False    # True if portfolio guards rejected it


def category_from_slug(slug: str | None) -> str:
    """Map a Polymarket market slug to one of FEES_BY_CATEGORY's keys.

    Conservative default: anything we can't classify falls into 'default'
    which uses the Sports fee schedule (0.75% peak).
    """
    if not slug:
        return "default"
    head = slug.split("-", 1)[0].lower()
    sport_heads = {"nba", "nfl", "mlb", "nhl", "atp", "wta", "soccer",
                   "epl", "champions", "ucl", "tennis", "ufc", "mma",
                   "golf", "f1", "sport", "sports"}
    if head in sport_heads:
        return "sports"
    if head in {"btc", "eth", "crypto", "sol", "doge", "altcoin"}:
        return "crypto"
    if head in {"politics", "election", "potus", "senate", "house",
                "trump", "biden", "harris", "vance"}:
        return "politics"
    if head in {"weather", "hurricane", "tornado", "rainfall"}:
        return "weather"
    if head in {"iran", "russia", "ukraine", "israel", "china",
                "geopolitics", "war"}:
        return "geopolitics"
    return "default"


def fee_for_category(category: str) -> float:
    """Look up the peak one-way taker fee for a category."""
    return FEES_BY_CATEGORY.get(category, FEES_BY_CATEGORY["default"])


def maker_rebate_for_category(category: str) -> float:
    """Look up the maker-side rebate fraction (% of taker fee captured)."""
    return MAKER_REBATE_BY_CATEGORY.get(
        category, MAKER_REBATE_BY_CATEGORY["default"],
    )


def round_trip_friction(
    category: str,
    *,
    entry_is_maker: bool = False,
    exit_is_maker: bool = False,
) -> float:
    """Effective round-trip friction for a copy trade given execution mode.

    Both legs taker → 2 x peak fee.
    One leg maker → peak fee + peak fee * (1 - rebate).
    Both legs maker → 2 x peak fee * (1 - rebate).
    """
    fee = fee_for_category(category)
    rebate = maker_rebate_for_category(category)
    entry_cost = fee * (1 - rebate) if entry_is_maker else fee
    exit_cost = fee * (1 - rebate) if exit_is_maker else fee
    return entry_cost + exit_cost


# Whether to size as if we capture the maker rebate. Kept FALSE because the
# maker-routing shadow observer (maker_router) reports a ~0% maker-fill rate as of
# 2026-05-30 — every maker attempt falls back to taker, so we capture no rebate.
# Sizing on the rebate we don't earn would be optimistic. Flip to True once the
# shadow shows a meaningful maker-fill rate, then re-validate.
SIZE_ON_MAKER_FIRST = False


def expected_sizing_friction(category: str) -> float:
    """Round-trip friction to size Kelly on, per category.

    This is the Cycle 5 "switch from a blanket 1.5% to per-category / per-leg"
    change: Geopolitics is fee-free (0%), Crypto is ~3.6% vs Sports 1.5%. It stops
    penalizing fee-free whales for phantom friction and correctly tightens on
    high-fee categories.

    We use TAKER friction (SIZE_ON_MAKER_FIRST=False) because the shadow observer
    currently sees ~0% maker fills — assuming the maker rebate in sizing would be
    optimistic until those fills actually materialize.
    """
    return round_trip_friction(
        category,
        entry_is_maker=SIZE_ON_MAKER_FIRST,
        exit_is_maker=SIZE_ON_MAKER_FIRST,
    )


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

    When `category_proxy` is given, also enforce the per-category deployment cap
    (Cycle 5): no single category may exceed MAX_CATEGORY_DEPLOY_PCT of bankroll.
    Each open bet's category is derived on the fly from its market_slug, so no
    schema change is needed.
    Returns (allowed, reason).
    """
    open_rows = conn.execute(
        "SELECT market_slug, cost_usd FROM poly_paper_bets "
        "WHERE source = 'whale_copy' AND settled_at IS NULL"
    ).fetchall()
    open_n = len(open_rows)
    deployed = sum(float(b["cost_usd"] or 0) for b in open_rows)

    if open_n >= MAX_OPEN_POSITIONS:
        return False, f"max_open_positions ({open_n} >= {MAX_OPEN_POSITIONS})"

    if deployed + proposed_stake > bankroll_usd * MAX_PORTFOLIO_DEPLOY_PCT:
        return False, (
            f"portfolio_deploy_cap "
            f"(deployed ${deployed:.2f} + ${proposed_stake:.2f} > "
            f"{MAX_PORTFOLIO_DEPLOY_PCT * 100:.0f}% of ${bankroll_usd:.0f})"
        )

    # Per-category deployment cap: keep any single category ≤ 25% of bankroll
    # so we don't pile the whole book into, say, MLB on a busy night.
    if category_proxy:
        cat_deployed = sum(
            float(b["cost_usd"] or 0)
            for b in open_rows
            if category_from_slug(b["market_slug"]) == category_proxy
        )
        cat_cap = bankroll_usd * MAX_CATEGORY_DEPLOY_PCT
        if cat_deployed + proposed_stake > cat_cap:
            return False, (
                f"category_deploy_cap "
                f"({category_proxy}: ${cat_deployed:.2f} + ${proposed_stake:.2f} > "
                f"{MAX_CATEGORY_DEPLOY_PCT * 100:.0f}% of ${bankroll_usd:.0f})"
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
