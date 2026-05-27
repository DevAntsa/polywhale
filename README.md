<p align="center">
  <img src="assets/banner.png" alt="polywhale" width="850">
</p>

<p align="center">
  <strong>Truth is liquid. Bet on everything.</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/status-live%20on%20production-success" alt="status">
  <img src="https://img.shields.io/badge/tests-54%20passing-brightgreen" alt="tests">
  <img src="https://img.shields.io/badge/python-3.11+-blue?logo=python&logoColor=white" alt="python">
  <img src="https://img.shields.io/badge/venue-Polymarket-7c3aed" alt="polymarket">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="license"></a>
</p>

---

## What this is

**polywhale** is a sharp-money detection and arbitrage bot for [Polymarket](https://polymarket.com), the decentralized prediction market on Polygon. It does three things:

1. **Identifies the small group of wallets that consistently win** on Polymarket and surfaces their new positions in real time via Telegram so you can follow their trades.
2. **Detects mathematical arbitrage** across negative-risk event groups (FIFA World Cup, NBA Champion, presidential nominees) where the outcome prices temporarily sum to less than $1.
3. **Paper-trades both signals against real prices** to measure edge before any real capital is deployed.

All from public Polymarket APIs. No scraping, no anti-bot evasion, no ToS landmines.

---

## The unique insight

Most retail copy-trading bots assume *all profitable Polymarket wallets are worth copying*. They aren't. Two distinct archetypes show up on the leaderboard, and **only one of them has alpha you can capture**:

| Archetype | Margin (profit/volume) | What they do | Worth copying? |
|---|---|---|---|
| **Directional sharp** | >= 3% | Predicts outcomes accurately, holds positions to resolution | YES, picks have edge |
| **Arb operator** | ~1% | Takes both sides of mispricings; profit = bid/ask spread | NO, copying one leg loses the hedge |

polywhale's classifier separates them automatically using profit and volume from Polymarket's leaderboard API. The default watchlist tracks **15 confirmed sharps** combining our top-margin picks with a curated list from polywhaler.com's skill ranking.

---

## Live demo

```text
$ polywhale poly-whales
Classified 50 wallet(s) over window=30d.
  sharps=5  arb_ops=5  hybrids=1  unknown=39

Top sharps (worth copying):
  surfandturf               0x9f2f...2ca8  margin= 13.9%  profit=$ 2,971,286
  bossoskil1                0xa5ea...d96a  margin=  4.9%  profit=$ 2,841,023   (MLB specialist)
  VPenguin                  0xfbf3...b218  margin=  6.0%  profit=$ 1,628,257

Top arb operators (DO NOT copy):
  Countryside               0xbddf...c684  margin=  1.4%  profit=$ 1,686,735
  swisstony                 0x204f...5e14  margin=  1.7%  profit=$ 1,893,974

$ polywhale poly-arbs --event-slug 2026-fifa-world-cup-winner-595 --inspect-only
Event: 2026-fifa-world-cup-winner-595
  title          : 2026 FIFA World Cup Winner
  neg-risk legs  : 48
  sum(best_ask)  : 1.0640
  raw edge       : -6.40%   (overround now; arb appears when sum drops below 1.00)

$ polywhale pulse
=== polywhale pulse ===
  whales tracked       : 15
  whale snapshots      : 4,720
  whale signals        : 23
  book snapshots       : 480
  combo arbs detected  : 0
  paper bets total     : 0  (settled: 0)
  paper P&L            : $+0.00
```

When a sharp opens a new position, you get a Telegram message:

```text
NEW BET by whale 0xa5ea13a81d...
  Yankees vs Red Sox
  outcome: Yankees
  size: 100,000
  current price: 0.430
```

---

## Architecture

```text
                    +---------------------------------+
                    |   Polymarket public REST APIs   |
                    |  gamma · clob · data · lb       |
                    +----------------+----------------+
                                     |
        +---------------+------------+------------+----------------+
        v               v            v            v                v
   book watcher    whale watcher  arb scanner  leaderboard    paper trader
   (CLOB /book)    (data-api)     (gamma+CLOB) (lb-api)       (gamma resolve)
        |               |            |            |                |
        +---------------+------------+------------+----------------+
                                     |
                                     v
                              +-------------+
                              |   SQLite    |
                              +------+------+
                                     |
              +----------------------+----------------------+
              v                      v                      v
        whale-diff           combo-arb detector       pulse / P&L
        engine                                             dashboard
              |
              v
        Telegram alerts
```

Single-file SQLite, `httpx` for sync I/O, `click` for CLI. No queues, no Kafka, no Redis. Runs on a 4 EUR/mo VPS.

---

## CLI surface

| Command | Purpose |
|---|---|
| `polywhale migrate` | Apply schema migrations |
| `polywhale poly-whales` | Classify leaderboard wallets as sharp / arb_op / hybrid |
| `polywhale poly-markets [--show-skew]` | List top Polymarket markets by 24h volume |
| `polywhale poly-book --slug X` | Print order book depth for a market |
| `polywhale poly-watch --default` | Poll order books for tracked markets |
| `polywhale poly-arbs --event-slug X` | Scan a neg-risk event for combinatorial arbs |
| `polywhale whale-snapshot --wallet X` | Pull a wallet's current open positions |
| `polywhale whale-watch --default` | Poll multiple wallets on an interval |
| `polywhale whale-signals --default --alert` | Diff snapshots and push Telegram alerts on new sharp positions |
| `polywhale poly-paper-combo --event-slug X` | Paper-bet a combo arb at current ask prices |
| `polywhale poly-paper-bet --slug X --side YES --shares N` | Manual directional paper bet |
| `polywhale poly-paper-settle` | Settle resolved paper bets via gamma + compute P&L |
| `polywhale poly-paper-pulse` | Per-source P&L breakdown |
| `polywhale pulse` | At-a-glance status snapshot |

---

## Quick start

```bash
git clone https://github.com/DevAntsa/polywhale.git
cd polywhale

# Install (Python 3.11+)
python3 -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# Configure
cp .env.example .env
# Edit .env with Telegram bot token + chat ID (optional; only for --alert)

# Initialize the database
polywhale migrate

# Take a first look
polywhale poly-whales
polywhale poly-markets --limit 10
polywhale pulse
```

---

## Production deployment

`deploy/` contains everything to run polywhale unattended on a VPS:

- **`install.sh`** - idempotent bootstrap (venv, install, migrate, sanity checks)
- **`systemd/*.service` + `*.timer`** - five timer-driven oneshot units with `MemoryMax=300M` + `CPUQuota=50%` so the bot can never starve other workloads
- **`disable_all.sh`** - one-shot kill switch with zero impact on co-hosted services
- **`health_check.sh`** - 4-stage audit (services, timer schedules, log freshness, error scan)
- **`README.md`** - phased deploy guide with verify-gates after each step

Five timers handle the workload:

| Timer | Cadence | What it does |
|---|---|---|
| `poly-watch` | 30 min | Order-book depth snapshots |
| `whale-watch` | 30 min | Whale position snapshots |
| `whale-signals` | 90 min | Diff snapshots + Telegram alert |
| `poly-arbs` | 10 min | Combinatorial arb scan |
| `poly-paper-settle` | daily 23:00 UTC | Settle resolved paper bets |

Verified live on a co-hosted Hetzner CAX11 — ~260 MB peak RAM, ~6 GB/year disk, idle CPU.

---

## Strategy notes

**Why sharp-money detection and not just arbitrage?**

Pure arbitrage on Polymarket is brutal: the simple YES + NO < $1 mispricings get picked off in seconds by latency-optimized bots running co-located on Polygon validators. Retail can't win that race.

What retail *can* do is be patient. Two genuine retail-friendly opportunities exist:

- **Slow combinatorial arbs.** In multi-outcome neg-risk groups (e.g. the 48 outcomes of "Who wins the World Cup?"), the sum across all YES tokens occasionally drifts below $1 during low-attention periods. Polywhale scans these continuously.
- **Sharp-money copy signals.** A handful of wallets (5 or fewer in any given month) consistently beat the market. They're not unbeatable, they just make better predictions than the average bettor. Their new positions are leading indicators.

The bet is that combining "the cheapest combinatorial arbs the fast bots miss" with "directional follow on sharps' new positions" produces a positive-edge portfolio at retail scale. The paper-trading layer measures this empirically before any real money moves.

---

## Project status

- [x] **Whale classifier** - separates 15 sharps from the noise via margin-ranking
- [x] **Position-diff detector** - Telegram alerts on new sharp moves
- [x] **Combinatorial arb detection** - scans neg-risk event groups
- [x] **Paper trading layer** - records would-be bets, settles via gamma, computes per-source P&L
- [x] **Production deploy artifacts** - systemd timers, kill switch, 4-stage health audit
- [x] **Live deployment** - running on Hetzner co-host since 2026-05-27
- [ ] **Real execution** via `py-clob-client` (EIP-712 signed orders) - needs funded Polygon wallet
- [ ] **Auto-refresh watchlist** weekly from leaderboard
- [ ] **Backtesting harness** - replay historical book snapshots against detection logic
- [ ] **Strategy attribution dashboard** - which signal source actually makes money

---

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Ecosystem + type hints |
| HTTP | `httpx` | Sync API with proper timeout handling |
| CLI | `click` | Composable subcommands + auto-help |
| Storage | SQLite (WAL mode) | Single-file, zero ops, sufficient for the volume |
| Lint / format | `ruff` | Fast unified linter + formatter |
| Tests | `pytest` | 54 tests covering math, persistence, mocking the four APIs |
| Process supervision | `systemd` timers | Kernel-enforced resource caps + journalctl observability |
| Alerts | Telegram Bot API | Free, push to phone, no infrastructure |

No databases beyond SQLite. No message queues. No external orchestration. Designed to run on a single small VPS.

---

## Acknowledgments

- **Saguillo, Ghafouri, Kiffer, Suárez-Tangil (2025)**, *Unravelling the Probabilistic Forest* - empirical foundation showing ~$40M extracted in Polymarket arbs over 12 months, top 3 wallets earning $4.2M. The two-archetype classification (market rebalancing vs combinatorial) inspired the dual-track design here. ([arXiv](https://arxiv.org/abs/2508.03474))
- **[Polymarket's developer docs](https://docs.polymarket.com)** - clean public APIs with no auth required for read access.
- **[polywhaler.com](https://polywhaler.com)** - their leaderboard contributed 10 of the 15 default sharps in the watchlist (separately verified via the polymarket leaderboard API).

---

## Disclaimer

This software is for research and educational purposes. Prediction-market trading involves substantial risk, including total loss of principal. polywhale is **not financial advice**, and historical signals do not guarantee future returns. The author assumes no liability for any financial losses incurred from use of this code. Verify local regulations before using prediction markets in your jurisdiction.

---

## License

[MIT](LICENSE) - use freely, modify as you wish, attribute if you publish derivatives.
