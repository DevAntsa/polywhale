"""Survival-score computation for the whale watchlist.

Driven by Cycle 3 research (2026-05-29) — Akey et al. 2026 (SSRN 6443103) and
Yang 2026 (SSRN 6556613). 44% of in-sample skilled traders retain skilled status
out-of-sample. Survival features ranked by evidence strength:

  1. Sample size n_resolved          (25 pts, saturates at n=50)
  2. Maker-vs-taker share            (25 pts, saturates at 60%)   [PLACEHOLDER]
  3. Market breadth with focus       (20 pts, peak at 2-6 cats)
  4. Recovery factor (cum/|max_dd|)  (15 pts, saturates at 3)
  5. Return skew                     (15 pts, positive sustainable)

Maker-share is the single most causal feature in the literature but we do not
currently collect per-whale maker/taker breakdown — that data lives in the
Polygon trade events and would need a separate ingestor. Until then, the
implementation accepts maker_share as an external input (default 0.5 = neutral)
so the rest of the score still works.

Score interpretation (per Cycle 3 research):
  >= 50  : full Kelly stake
  35-50  : size-cut to 50%
  < 35 AND n_resolved >= 30 : auto-drop candidate (require 2 consecutive
                              weekly recomputes below threshold to fire)
  n_resolved < 30           : probation, cap at 25% size regardless of score

References:
  - Akey, Grégoire, Harvie, Martineau 2026 (SSRN 6443103)
  - Yang 2026 (SSRN 6556613)
  - Capital Spectator Research Review Apr 2026
"""

from __future__ import annotations

import logging
import math
import sqlite3
import statistics
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Sub-score weight constants. Sum == 100 so survival_score is in [0,100].
W_SAMPLE = 25
W_MAKER = 25
W_BREADTH = 20
W_RECOVERY = 15
W_SKEW = 15

# Drop / size thresholds — see Cycle 3 research.
HARD_DROP_SCORE = 35
SIZE_CUT_SCORE = 50
PROBATION_N = 30
SHADOW_REINSTATE_SCORE = 55


@dataclass(frozen=True)
class SurvivalBreakdown:
    wallet: str
    n_resolved: int
    f_sample: float
    f_maker: float
    f_breadth: float
    f_recovery: float
    f_skew: float
    survival_score: float
    maker_share: float
    n_categories: int


def _f_sample(n_resolved: int) -> float:
    return W_SAMPLE * min(1.0, n_resolved / 50.0)


def _f_maker(maker_share: float) -> float:
    return W_MAKER * min(1.0, max(0.0, maker_share) / 0.6)


def _f_breadth(n_categories: int) -> float:
    return W_BREADTH * max(0.0, 1.0 - abs(n_categories - 4) / 4.0)


def _f_recovery(cum_pnl: float, max_dd: float) -> float:
    if max_dd >= 0:
        return W_RECOVERY if cum_pnl > 0 else 0.0
    ratio = max(0.0, cum_pnl / abs(max_dd))
    return W_RECOVERY * min(1.0, ratio / 3.0)


def _f_skew(skew: float | None) -> float:
    if skew is None:
        return W_SKEW * 0.5
    return W_SKEW * max(0.0, min(1.0, (skew + 1.0) / 2.0))


def _sample_skew(returns: list[float]) -> float | None:
    n = len(returns)
    if n < 3:
        return None
    mean = statistics.fmean(returns)
    var = statistics.pvariance(returns, mu=mean)
    if var <= 0:
        return None
    std = math.sqrt(var)
    third = sum((r - mean) ** 3 for r in returns) / n
    return third / (std ** 3)


def _max_drawdown(pnls_chronological: list[float]) -> float:
    """Return the most negative running-sum drop. 0.0 if never went underwater."""
    running = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnls_chronological:
        running += pnl
        peak = max(peak, running)
        dd = running - peak
        max_dd = min(max_dd, dd)
    return max_dd


def _category_from_market_slug(slug: str | None) -> str | None:
    """Coarse category extraction. Polymarket slugs lead with a sport/topic tag."""
    if not slug:
        return None
    head = slug.split("-", 1)[0].lower()
    sports = {"nba", "nfl", "mlb", "nhl", "atp", "wta", "soccer", "epl",
              "champions", "ucl", "tennis", "ufc", "mma", "golf", "f1"}
    if head in sports:
        return "sports"
    if head in {"will", "what", "who", "when"}:
        return "events"
    if head in {"btc", "eth", "crypto", "sol", "doge"}:
        return "crypto"
    return head


def compute_survival_score(
    conn: sqlite3.Connection,
    wallet: str,
    *,
    maker_share: float = 0.5,
) -> SurvivalBreakdown:
    """Compute survival score from settled poly_paper_bets for this wallet.

    maker_share defaults to 0.5 (neutral, no data) — pass the observed value
    when the maker/taker ingestor is built.
    """
    rows = conn.execute(
        """
        SELECT b.pnl_usd, b.market_slug
        FROM poly_paper_bets b
        JOIN whale_signals s ON s.signal_id = b.source_ref_id
        WHERE s.wallet = ? AND b.settled_at IS NOT NULL
        ORDER BY b.settled_at ASC
        """,
        (wallet.lower(),),
    ).fetchall()

    n_resolved = len(rows)
    pnls = [float(r["pnl_usd"] or 0.0) for r in rows]
    cum_pnl = sum(pnls)
    max_dd = _max_drawdown(pnls)
    categories = {_category_from_market_slug(r["market_slug"]) for r in rows}
    categories.discard(None)
    n_categories = len(categories)
    skew = _sample_skew(pnls)

    f_sample = _f_sample(n_resolved)
    f_maker = _f_maker(maker_share)
    f_breadth = _f_breadth(n_categories)
    f_recovery = _f_recovery(cum_pnl, max_dd)
    f_skew_v = _f_skew(skew)
    score = f_sample + f_maker + f_breadth + f_recovery + f_skew_v

    return SurvivalBreakdown(
        wallet=wallet.lower(),
        n_resolved=n_resolved,
        f_sample=f_sample,
        f_maker=f_maker,
        f_breadth=f_breadth,
        f_recovery=f_recovery,
        f_skew=f_skew_v,
        survival_score=score,
        maker_share=maker_share,
        n_categories=n_categories,
    )


def persist_survival_score(
    conn: sqlite3.Connection,
    breakdown: SurvivalBreakdown,
) -> None:
    conn.execute(
        """
        UPDATE whale_watchlist
        SET survival_score = ?,
            survival_score_at = ?,
            survival_n_resolved = ?,
            survival_maker_share = ?
        WHERE wallet = ?
        """,
        (
            round(breakdown.survival_score, 2),
            int(time.time()),
            breakdown.n_resolved,
            breakdown.maker_share,
            breakdown.wallet,
        ),
    )
    conn.commit()


def recompute_all(
    conn: sqlite3.Connection,
    *,
    maker_share_overrides: dict[str, float] | None = None,
) -> list[SurvivalBreakdown]:
    """Recompute and persist survival score for every active watchlist wallet."""
    overrides = maker_share_overrides or {}
    wallets = [r["wallet"] for r in conn.execute(
        "SELECT wallet FROM whale_watchlist WHERE active = 1"
    )]
    out = []
    for w in wallets:
        ms = overrides.get(w, 0.5)
        b = compute_survival_score(conn, w, maker_share=ms)
        persist_survival_score(conn, b)
        out.append(b)
        logger.info(
            "survival_score wallet=%s n=%d score=%.1f "
            "(sample=%.1f maker=%.1f breadth=%.1f recovery=%.1f skew=%.1f)",
            w[:14], b.n_resolved, b.survival_score,
            b.f_sample, b.f_maker, b.f_breadth, b.f_recovery, b.f_skew,
        )
    return out


def survival_tier(breakdown: SurvivalBreakdown) -> str:
    """Categorize a survival breakdown into a tier name for routing decisions."""
    if breakdown.n_resolved < PROBATION_N:
        return "probation"
    if breakdown.survival_score < HARD_DROP_SCORE:
        return "drop-candidate"
    if breakdown.survival_score < SIZE_CUT_SCORE:
        return "size-cut"
    return "full-size"
