<p align="center">
  <img src="assets/banner.png" alt="polywhale" width="850">
</p>

<p align="center">
  <em>Truth is liquid. Bet on everything.</em>
</p>

<p align="center">
  <a href="#what-it-does"><img src="https://img.shields.io/badge/tests-passing-brightgreen" alt="tests"></a>
  <a href="#quick-start"><img src="https://img.shields.io/badge/python-3.11+-blue" alt="python"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="license"></a>
  <img src="https://img.shields.io/badge/venue-Polymarket-purple" alt="polymarket">
</p>

# polywhale

> A Polymarket whale-tracking and combinatorial arbitrage bot. Detects sharp-money signals from top-profitable wallets, finds mathematically guaranteed arbitrage opportunities across negative-risk market groups, and paper-trades the results against real prices — all from the public Polymarket APIs, no scraping.

## What it does

Polymarket is a decentralized prediction market on Polygon where users bet on real-world outcomes. polywhale exploits two structural inefficiencies:

1. **Sharp-money copy signals.** A handful of wallets are consistently profitable by predicting outcomes accurately. polywhale identifies them via a margin-based classifier (separating *directional sharps* with high profit-per-volume from *arb operators* who simply take both sides of mispricings), tracks their positions in real time, and pushes Telegram alerts when they open new positions worth following.
2. **Combinatorial arbitrage on negative-risk groups.** For events like "Who wins the FIFA World Cup?" the YES tokens across all outcomes should sum to $1.00. When the order book temporarily prices them below $1.00 (after fees), buying the full set locks in a guaranteed payout. polywhale scans these groups continuously.

All findings are paper-traded first against the real Polymarket order book to validate detection math before committing real capital.

## Quick demo

```bash
# Classify the top 50 leaderboard wallets
$ polywhale poly-whales
Classified 50 wallet(s) over window=30d.
  sharps=5  arb_ops=5  hybrids=1  unknown=39

Top sharps (worth copying):
  surfandturf               0x9f2f...  margin= 13.9%  profit=$ 2,971,286
  bossoskil1                0xa5ea...  margin=  4.9%  profit=$ 2,841,023  (MLB specialist)
  VPenguin                  0xfbf3...  margin=  6.0%  profit=$ 1,628,257

Top arb operators (DO NOT copy):
  Countryside               0xbddf...  margin=  1.4%  profit=$ 1,686,735  (takes both sides)

# Scan a negative-risk event for combinatorial arbs
$ polywhale poly-arbs --event-slug 2026-fifa-world-cup-winner-595 --inspect-only
Event: 2026-fifa-world-cup-winner-595
  title          : 2026 FIFA World Cup Winner
  neg-risk legs  : 48
  sum(best_ask)  : 1.0640
  raw edge       : -6.40%   (currently overround; arb appears when sum drops below 1.00)
```

## Architecture

```
                          Polymarket public APIs
                          (gamma, clob, data, lb)
                                   │
                                   ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│ Whale snap   │  │ Book watch   │  │ Combo arb    │  │ Whale class  │
│ (data-api)   │  │ (CLOB book)  │  │ (gamma+CLOB) │  │ (lb-api)     │
└──────┬───────┘  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘
       │                 │                 │                 │
       ▼                 ▼                 ▼                 ▼
       └─────────────► SQLite ◄─────────────────────────────┘
                        │
       ┌────────────────┴────────────────┐
       │                                 │
       ▼                                 ▼
  Whale diff →→→→→→→→→→→→→→→→→ Paper trader →→→→ Settlement
       │                       (records P&L)    (via gamma)
       ▼
  Telegram alerts
```

- **SQLite** — single-file storage, no external DB needed. Sport-agnostic schema works for any prediction-market venue.
- **httpx** — sync HTTP client with timeouts + retries.
- **click** — CLI surface; each command is also callable as a library function for testing.
- **systemd** — production deployment uses timer-driven oneshot units with kernel-enforced memory/CPU caps (see `deploy/`).

## Features

- **Whale classifier** — distinguishes directional sharps (worth copying) from arb operators (worth avoiding) using profit/volume margin from the official leaderboard.
- **Position diff detector** — emits `new_position`, `added_size`, `closed_position`, `reduced_size` signals when whale portfolios change.
- **Telegram alerts** — push notifications for actionable signals; idempotent so the same signal never fires twice.
- **Combinatorial arb detector** — handles negative-risk event groups (FIFA World Cup, NBA Champion, presidential nominees) where outcomes sum to $1.
- **Paper trading layer** — records would-be bets at real ask prices; settles via gamma when markets resolve; computes per-source P&L (combo arb vs whale copy vs manual).
- **Production deploy** — five systemd timers cover order-book watching, whale-snapshotting, signal diffing, arb scanning, and daily settlement. Includes one-shot kill switch and 4-stage health audit.

## Quick start

```bash
git clone https://github.com/DevAntsa/polywhale.git
cd polywhale

# Install (Python 3.11+)
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# Configure
cp .env.example .env
# Edit .env with your Telegram bot token + chat ID (optional; used for alerts)

# Initialize the database
polywhale migrate

# First scans
polywhale poly-whales                # see who the sharps are
polywhale poly-markets --limit 10   # see top active markets
polywhale poly-arbs --event-slug 2026-fifa-world-cup-winner-595 --inspect-only

# Take some whale snapshots (zero-cost; uses Polymarket's free APIs)
polywhale whale-snapshot --wallet 0xa5ea13a81d2b7e8e424b182bdc1db08e756bd96a

# After 30 minutes pass and you take another snapshot, diff for signals:
polywhale whale-signals --default

# Paper-trade a combo arb opportunity (uses real prices, no money on the line)
polywhale poly-paper-combo --event-slug 2026-fifa-world-cup-winner-595 --total-stake 100

# Status snapshot
polywhale pulse
```

## Strategy notes

The bot is built on a clear distinction between two profitable wallet archetypes visible in Polymarket leaderboards:

| Archetype | Margin (profit/volume) | Strategy | Worth copying? |
|---|---|---|---|
| **Directional sharp** | >= 3% | Predicts outcomes accurately, bets meaningfully sized positions | YES — their picks have alpha |
| **Arb operator** | ~1% | Takes both sides of mispriced markets; profit = bid/ask spread | NO — copying one leg = lose the hedge |

The whale classifier uses this margin threshold to separate them automatically. Default watchlist combines our top 5 margin-ranked sharps with 10 high-skill wallets from Polywhaler's leaderboard.

For combinatorial arbs, the bot looks for the rare moments when a negative-risk event's outcome prices temporarily underprice the full set. Empirical reality: these arbs are mostly picked off within seconds by latency-optimized bots. polywhale doesn't try to win the latency race — it captures the second-tier arbs in less-watched markets and the slower-moving cross-venue mispricings.

See `docs/STRATEGY.md` (TODO) for the full economic model and academic grounding.

## Deployment

`deploy/` contains everything needed to run polywhale unattended on a small VPS (€4/mo Hetzner CAX11 is sufficient):

- `install.sh` — idempotent bootstrap (venv, install, migrate, smoke test)
- `systemd/*.service` + `*.timer` — five timer-driven oneshot units, each with `MemoryMax=300M` and `CPUQuota=50%`
- `disable_all.sh` — one-shot kill switch (no impact on co-hosted services)
- `health_check.sh` — 4-stage audit (units, log freshness, errors, isolation)
- `README.md` — phased deploy guide

The deploy is designed to be co-hosted alongside other workloads without resource contention.

## Roadmap

- [x] Whale classification + watchlist
- [x] Position diff detector with Telegram alerts
- [x] Combinatorial arbitrage detection
- [x] Paper trading layer
- [x] Production deploy artifacts (systemd timers, health audit, kill switch)
- [ ] Real execution via `py-clob-client` (EIP-712 signed orders) — requires funded Polygon wallet
- [ ] Auto-update watchlist (refresh sharps weekly from leaderboard API)
- [ ] Backtesting harness (replay historical book snapshots against detection logic)
- [ ] Strategy attribution dashboard (which signal sources actually make money)

## Disclaimer

This software is for research and educational purposes. Prediction-market trading involves substantial risk, including total loss of principal. polywhale is not financial advice, and historical signals do not guarantee future returns. The author assumes no liability for any financial losses incurred from use of this code.

## License

MIT — see `LICENSE`.
