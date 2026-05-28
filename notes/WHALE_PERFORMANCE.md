# Whale Wallet Performance Journal

Running log of how each tracked whale is performing in our paper copy-trade system.
Used to decide who stays on the watchlist, who gets dropped, and what criteria
should eventually be encoded into `whale_refresh.py` for auto-prune.

This is a working document — update with each meaningful PnL milestone.

---

## Live snapshot — 2026-05-28 morning (~36h since bot went live)

**System state:** 85 closed copy bets, +$56.25 realized, 129 open at $5,140
deployed (bankroll cap engaged, drains naturally).

| Whale (label / wallet prefix) | Closed bets | W / L | PnL | Notes |
|---|---|---|---|---|
| **bossoskil1** `0xa5ea...d96a` | 5 | 3 / 2 | **+$74.97** | MLB specialist. Carrying the entire portfolio. |
| strike123 `0xf284...b9f9` | 7 | 3 / 4 | +$22.01 | Mixed, slightly positive. Hold. |
| wokerjoesleeper `0x63d4...a2f1` | 28 | 0 / 28 | $0.00 | 576 sigs/30d (most active). All closes at entry price → no edge captured at our cadence. |
| wan123 `0xde7b...5f4b` | 27 | 0 / 27 | $0.00 | Same pattern: very high signal volume, in-and-out at same price. |
| (unnamed) `0x2a2c...9bc1-...` | 3 | 0 / 3 | $0.00 | Flat. |
| (unnamed) `0x73e3...3239` (ID4) | 1 | 0 / 1 | $0.00 | One trade, flat. |
| PineBluff `0x1341...0853` | 2 | 0 / 2 | $0.00 | Flat. Dormancy 14d flag is already on. |
| Erasmus `0xc658...b784` | 1 | 0 / 1 | -$5.95 | One loss so far. Too early to judge. |
| (unnamed) `0x2c33...0563-...` | 11 | 1 / 10 | **-$34.80** | Worst contributor. High volume, mostly losing. |

**Concentration finding:** bossoskil1 alone is responsible for **133%** of our
realized PnL ($74.97 of $56.25). The rest of the watchlist nets to a loss.

## Patterns to investigate

### Pattern A — "in-and-out at $0 PnL" wallets
- **wokerjoesleeper** and **wan123** between them: 55 closed bets, 0 wins, 0 dollars
- These whales open positions and close them within the same poll window
  (60s cadence), so we see entry+exit signals fire back-to-back with the same
  `current_price` → close_copy_bet computes PnL = (exit - entry) * shares = 0
- This is a SYSTEM issue, not a whale-quality issue. The whales might genuinely
  have edge; we just can't capture it at 60-second polling cadence.
- Possible fixes (future): increase poll cadence, or weight signals by whale's
  hold duration history.

### Pattern B — bossoskil1's outsized contribution
- 5 closed bets, 3 wins at sizes that overwhelm losses
- Win prices: 0.885 → 1.000 (MLB), 0.715 → 1.000 (tennis), one other big win
- Specialty: sports markets, particularly MLB and tennis
- This whale alone validates the entire "follow the sharps" thesis

### Pattern C — 0x2c33...0563 going wrong
- 11 closed bets, 1 win, 10 losses, **-$34.80**
- High volume (214 signals/30d, top 8 on activity rank)
- Wide loss spread suggests they don't have the edge they appear to have
  on the leaderboard, OR they're an arb operator we mis-classified as a sharp
- Worth investigating their margin/volume profile vs the bot's classifier

---

## Criteria we'll eventually encode

These will become the auto-prune thresholds in `whale_refresh.py` once we have
30-60 days of data. Right now they're rough targets to develop a feel for.

**Drop a whale if** (any of):
- Closed bet count ≥ 25 AND realized PnL ≤ $0 (proven not to add edge at our cadence)
- Closed bet count ≥ 10 AND WR < 10% AND total PnL < -$20 (negative and losing money)
- Signal density > 200/30d AND average PnL per close = $0 ± $0.50 (no captured edge)

**Keep a whale if** (any of):
- Realized PnL > $50 over ≥ 5 closed bets (real alpha)
- Average win > 3 × average loss (asymmetric payoff confirmed)
- Recent 7-day PnL positive (still working)

**Boost stake size on a whale if** (future Phase 2):
- Realized PnL > $200 over ≥ 20 closed bets AND consistent across categories
- This is the eventual feeding loop for `bot_wr_30d` column to weight stakes

---

## Decision log

Use this section to record human/AI decisions about the watchlist over time.

- **2026-05-28** — Live data shows 13 of 17 whales producing $0 or negative PnL
  after 36h. Not deciding to drop anyone yet (sample size too small for individuals).
  Will revisit on **2026-06-10** with 14 days of data.
- **2026-05-28** — `0x63d43bbb87f8` (wokerjoesleeper) and `0xde7be6d489bc` (wan123)
  flagged as "$0 PnL pattern" — keep tracking for now to confirm hypothesis that
  their edge isn't capturable at 60s cadence.
- **2026-05-28 (afternoon)** — Historical backfill landed 2,462 reconstructed
  episodes across 17 whales. Major findings: **saintQ has 100% WR over 25
  resolved episodes** (vastly better than our 18h sample suggested);
  **ExitLiquidty has 74% WR / +$95K over 27 resolved** (was dormant 27d in our
  live data but historically a top performer); **nojnn (0x7f9e) is silently
  losing $41K historical with 33% WR over 3 resolved** — we treated them as a
  sharp but they're not.

---

## Historical backtest baseline (2026-05-28, snapshot)

Reconstructed from `data-api/activity` pagination → vwap per-position
analysis. These are the WHALES' own PnL, not ours — but it tells us where
real edge exists and what our copy targets should be.

```
Wallet                  Resolved  WR     Whale PnL    Status
saintQ (0x1e3b)           25     100%    +$7.9K       TOP-TIER — track + copy
ExitLiquidty (0xeb67)     27      74%    +$95K        TOP-TIER — but dormant 27d
Erasmus (0xc658)          19      47%    +$18K        marginal — coin-flip ish
ID4 (0x73e3)              12      75%    +$3.3K       sample small but good ratio
VPenguin (0xfbf3)          7      86%    +$722K       small n, big numbers
EB99999 (0x5d0f)           4     100%    +$77K        small n, big numbers
nojnn (0x7f9e)             3      33%    -$41K        DROP CANDIDATE
bossoskil1 (0xa5ea)        0       -      open only   markets not resolved yet
strike123 (0xf284)         1       0%    -$90         not enough data
(other 9 whales)           0       -      open only   need time
```

### What this changes about the watchlist

- **High confidence keeps**: saintQ, ExitLiquidty, VPenguin, EB99999, ID4
- **Candidates to drop**: nojnn (negative across resolved sample),
  wokerjoesleeper + wan123 (still flagged for $0 pattern from live data)
- **Reserve judgment**: bossoskil1, strike123, the other 8 whose markets
  haven't resolved enough — re-evaluate in 4-6 weeks

---

## Validation methodology established (2026-05-28)

Three new CLI commands now part of the audit toolkit:

```bash
# Monte Carlo — overstated for our use; reference only
polywhale monte-carlo --per-whale --samples 10000

# Historical backtest — the credible per-whale edge measurement
polywhale historical-backtest --fee-pct 0.01

# Walk-forward — the credible out-of-sample PnL forecast
polywhale walk-forward --train-days 14 --test-days 7 --top-k 5
```

**Anchor on walk-forward, not Monte Carlo.** Walk-forward says $85/week
average across 17 windows with 58.8% consistency. Monte Carlo says $678/week
median with 98% positive probability. The walk-forward number is the one to
plan around — it's the only one that tests out-of-sample.

---

## 5-agent research findings (2026-05-28 evening)

After Monte Carlo + walk-forward shipped, five parallel research agents dove
into whale longevity, copy-trade crowding, whale identities, portfolio
construction, and paper-to-real friction. **Several findings contradicted
prior decisions and recalibrate our forecast.**

### The single most important finding

**`wokerjoesleeper` (0x63d43bbb87f8) is on Polymarket's OFFICIAL 26-address
smart-money list.** 81% WR over **42,902 predictions** targeting low-probability
markets. Our prior "$0 PnL pattern → drop" diagnosis was wrong. Their disciplined
Kelly sizing on undervalued NOs is too small per-trade to surface as PnL at our
60s polling cadence, but the underlying strategy is rigorous algo execution.

**Status as of 2026-05-28**: marked `endorsed=1` via `polywhale watchlist-endorse`,
exempt from auto-drop regardless of in-app PnL signals.

### Whale identity surfacing

| Whale | New intel | Action |
|---|---|---|
| wokerjoesleeper | Polymarket-26-list, 81% WR algo | Keep + endorsed |
| VPenguin | **@Vlad_kori on X** (public handle) | Monitor X timeline |
| kch123 | 2,932 trades / 87% sports / market-maker style | Highest legitimacy among non-dormant |
| Théo network | Theo4, Fredi9999, RepTrump, Len9311238 confirmed dormant since Nov 2024 (French ANJ regulatory action) | Stop tracking unless 2028 cycle |
| EB99999, nojnn, strike123 | Heavy on Iran markets — CNN/Bubblemaps flagged 93% Iran insider | Risk-flagged `iran-cluster` |
| bossoskil1 | Lost $2.36M in 8 days Jan 2026 | High variance, not skill — cap exposure |
| saintQ | 1,771 predictions, biggest win only $4.5K, 0 profile views | Likely low-stake bot, edge probably noise |

### Whale longevity (Akey et al. 2026 anchor)

The only rigorous Polymarket survivorship study:
- Only **12% of biggest-profit winners** pass a randomized skill test
- **~60% of "lucky winners" flip to losers** on held-out events
- Top-50 leaderboard turns over substantially — 2024 election names mostly
  gone from current monthly rankings

**Implication**: walk-forward $85/wk likely overstates by **30-50% at 12mo**,
**50-75% at 24mo** unless we actively rotate the watchlist quarterly.

### Paper-to-real friction (the hard reality check)

```
Polymarket round-trip fees (blended sports + politics): 1.4-1.8%
Slippage at $40 stake:                                  0-0.3%
Latency drag (60s cadence):                             0.3-0.7%
Other (failed fills, gas):                              0.2-0.3%
TOTAL realistic friction:                               ~2.6%

Our paper gross edge per trade:                         1.6% ($0.65/$40)
```

**Friction exceeds gross edge by 1pp** in realistic taker-only execution.
Mitigations: maker-side routing (resting limit orders for rebates), category
filtering to sports + politics, more selective signals.

**Realistic real-money expectation: $30-50/wk**, not $85/wk.

### Recalibrated timeline

- Path to $5K/mo income now **18-30 months** at $2K/mo salary additions
  (previously planned at 14-18 months)
- "Soon" means 18+ months, not 6
- Critical to build friction measurement BEFORE any real-money pilot

### Portfolio construction (Kelly fractional)

Recommended sizing formula (replaces flat $40 stake):
```
stake = bankroll × 0.25 × max(0, (μ − fees) / σ²)
```

Sample-size shrinkage:
- n < 10 trades: 0.5% exploration stake
- 10 ≤ n < 30: linear shrinkage `(n−10)/20` toward Kelly
- n ≥ 30: full 1/4 Kelly

Portfolio caps:
- Max 10 concurrent positions
- Max 25% bankroll deployed
- Max 25% per category
- Hard cap 2.5% bankroll per single bet
- Same-(market, side) dedup

Drop if: `μ − 2σ/√n ≤ −fees`

### Polymarket's 26-address smart-money specialists to add

These are endorsed by Polymarket itself with high category WR:
- HondaCivic — 85.7% weather
- cowcat — 88% Middle East geopolitics
- ewelmealt — ~100% soccer
- middleoftheocean — 83.1% soccer
- Big.Chungus — 73.7% box office
- George.Smiley — 76% AI/tech

Full wallet addresses in Odaily / Phemex coverage; pull via
`polywhale watchlist-add` then `polywhale watchlist-endorse`.

---

## How to refresh this file

Run from anywhere with the DB:

```python
# Get per-wallet performance (paste into a quick script when reviewing)
import sqlite3
conn = sqlite3.connect("/opt/polymarket/data/polywhale.sqlite")
conn.row_factory = sqlite3.Row
for r in conn.execute("""
    SELECT ws.wallet, COUNT(*) AS n,
           SUM(CASE WHEN pb.pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
           SUM(CASE WHEN pb.pnl_usd <= 0 THEN 1 ELSE 0 END) AS losses,
           ROUND(SUM(pb.pnl_usd), 2) AS pnl
    FROM poly_paper_bets pb
    JOIN whale_signals ws ON pb.source_ref_id = ws.signal_id
    WHERE pb.source = 'whale_copy' AND pb.settled_at IS NOT NULL
    GROUP BY ws.wallet ORDER BY pnl DESC
"""):
    label = conn.execute(
        "SELECT label FROM whale_watchlist WHERE wallet = ?", (r["wallet"],)
    ).fetchone()
    print(f"  {label['label'] if label and label['label'] else r['wallet'][:14]:<25}"
          f"  bets={r['n']:>3}  W/L={r['wins']}/{r['losses']:<3}  PnL=${r['pnl']:+.2f}")
```
