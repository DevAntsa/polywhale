# Polymarket bot â€” Hetzner deploy

Runs on a small ARM VPS, optionally co-hosted with other services. Poly footprint is small enough (~260 MB peak RAM, ~6 GB/year disk, idle CPU) that isolation isn't needed â€” but **hard isolation rules** still apply when sharing a box with anything production-critical. Set `$SERVER` to your host before running the commands below.

## Hard rules (do not violate)

1. **Never** edit anything under `/root/trading/`. Poly code lives at `/opt/polymarket/`.
2. **Never** use the conda `tflow` env. Poly has its own venv at `/opt/polymarket/venv`.
3. **Never** restart `devantsa-loop`, `devantsa-loop-acct2`, or `devantsa-liq`.
4. **Never** touch `/etc/systemd/system/devantsa-*.service`. Poly units use `poly-` or `whale-` prefixes.
5. After every phase: `systemctl is-active devantsa-loop devantsa-loop-acct2 devantsa-liq` â€” all 3 must remain `active`. If any flips, STOP.
6. If anything looks wrong: `/opt/polymarket/disable_all.sh` and report.

## Layout on the box

```
/opt/polymarket/
â”œâ”€â”€ app/                   git checkout of polywhale
â”œâ”€â”€ venv/                  dedicated Python venv
â”œâ”€â”€ data/                  SQLite database (polywhale.sqlite)
â”œâ”€â”€ logs/                  per-service log files
â”œâ”€â”€ disable_all.sh         rollback / kill switch
â””â”€â”€ health_check.sh        poly-side health audit
```

Systemd units installed at `/etc/systemd/system/{poly,whale}-*.{service,timer}`.

---

## Phase 0 â€” Pre-flight (always first)

```bash
# 1) In Hetzner Console: snapshot the box.
#    Servers â†’ [box] â†’ Snapshots â†’ Create Snapshot
#    Name: pre-poly-deploy-2026-05-27
#    Wait for "Healthy" before continuing.

# 2) SSH in and capture pre-state for diff:
ssh root@"$SERVER"
mkdir -p /root/deploy_snapshots/2026-05-27_pre_poly
systemctl list-units --type=service --state=running > /root/deploy_snapshots/2026-05-27_pre_poly/services_before.txt
df -h / > /root/deploy_snapshots/2026-05-27_pre_poly/disk_before.txt
free -h > /root/deploy_snapshots/2026-05-27_pre_poly/mem_before.txt

# 3) Verify trading is healthy now:
systemctl is-active devantsa-loop devantsa-loop-acct2 devantsa-liq
# Expected: active, active, active
```

## Phase 1 â€” Install

```bash
# All commands assume you're root on the Hetzner box.
mkdir -p /opt/polymarket/{data,logs}
cd /opt/polymarket
git clone https://github.com/DevAntsa/polywhale.git app
cd app

# Bootstrap script handles venv + pip install + initial .env.
bash deploy/install.sh
```

`install.sh` will:
- Create `/opt/polymarket/venv` using system Python 3.11+
- Install polywhale in editable mode (uses pyproject.toml entry points so `polywhale` is on PATH inside the venv)
- Write a placeholder `/opt/polymarket/app/.env` if none exists
- Run `polywhale migrate` to initialise SQLite at `/opt/polymarket/data/polywhale.sqlite`

After install, **edit `/opt/polymarket/app/.env`** and paste the same secrets as your local box:
- `ODDS_API_KEY=...` (unused but kept for compatibility)
- `TELEGRAM_BOT_TOKEN=...`
- `TELEGRAM_CHAT_ID=...`
- `POLYWHALE_DB_PATH=/opt/polymarket/data/polywhale.sqlite`

Then smoke test:

```bash
source /opt/polymarket/venv/bin/activate
polywhale migrate     # idempotent; ensures schema is current
polywhale poly-whales | tee /opt/polymarket/logs/smoke_test_$(date +%Y%m%d_%H%M%S).log
# Verify it pulled the leaderboard and printed sharps.
```

## Phase 2 â€” Install systemd units

```bash
# Copy unit files into systemd.
cp /opt/polymarket/app/deploy/systemd/*.service /etc/systemd/system/
cp /opt/polymarket/app/deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
```

## Phase 3 â€” Arm timers one at a time (with gate)

Bring up sequentially, verifying trading stays healthy between each.

`whale-fast` (60s cadence) supersedes `whale-watch` (30 min) + `whale-signals` (90 min) â€” it does
snapshot + diff + Telegram alert in one tight pass so we copy whales before other bots front-run us.
On a fresh deploy, enable `whale-fast` only; leave the slow pair disabled.

```bash
for unit in poly-watch whale-fast poly-arbs poly-paper-settle; do
  echo "=== Enabling $unit.timer ==="
  systemctl enable --now $unit.timer
  sleep 30
  echo "--- trading still active? ---"
  systemctl is-active devantsa-loop devantsa-loop-acct2 devantsa-liq
  echo "--- $unit timer next fire? ---"
  systemctl list-timers $unit.timer --no-pager
  echo "Press Enter to continue, Ctrl-C to stop"
  read
done
```

If migrating an existing deploy that already has `whale-watch.timer` + `whale-signals.timer` armed:

```bash
systemctl disable --now whale-watch.timer whale-signals.timer
systemctl enable --now whale-fast.timer
```

## Phase 4 â€” Health audit

```bash
chmod +x /opt/polymarket/app/deploy/health_check.sh
cp /opt/polymarket/app/deploy/health_check.sh /opt/polymarket/
/opt/polymarket/health_check.sh
```

Output should show:
- All 4 active timers `enabled` and scheduled (`poly-watch`, `whale-fast`, `poly-arbs`, `poly-paper-settle`)
- Log files fresh (whale-fast in particular should be <2 min old)
- No `ERROR` or `Traceback` lines in last 100 of any log

## Phase 5 â€” Verify rollback works

```bash
cp /opt/polymarket/app/deploy/disable_all.sh /opt/polymarket/
chmod +x /opt/polymarket/disable_all.sh

# Run it now while the system is fresh:
/opt/polymarket/disable_all.sh
# Verify trading services unaffected:
systemctl is-active devantsa-loop devantsa-loop-acct2 devantsa-liq

# Re-enable manually (confirms re-enable path works):
systemctl enable --now poly-watch.timer whale-fast.timer poly-arbs.timer poly-paper-settle.timer
```

Deploy complete. The bot now runs unattended.

## Day-to-day monitoring

```bash
# Quick status:
/opt/polymarket/health_check.sh

# Detailed logs from a unit:
journalctl -u poly-watch.service -n 50 --no-pager
tail -50 /opt/polymarket/logs/poly-watch.log

# Paper-trade P&L:
source /opt/polymarket/venv/bin/activate
polywhale poly-paper-pulse

# Manually run a one-shot scan:
polywhale poly-arbs --event-slug 2026-fifa-world-cup-winner-595 --inspect-only
```

## Updating the bot

When new code lands on GitHub `main`:

```bash
cd /opt/polymarket/app
git pull origin main
source /opt/polymarket/venv/bin/activate
pip install -e .          # in case dependencies changed
polywhale migrate              # apply new schema migrations
# Timers continue on their cadence; no service restart needed.
```

## Uninstall (clean removal)

```bash
/opt/polymarket/disable_all.sh
rm -f /etc/systemd/system/poly-*.{service,timer}
rm -f /etc/systemd/system/whale-*.{service,timer}
systemctl daemon-reload
rm -rf /opt/polymarket
```
