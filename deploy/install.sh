#!/bin/bash
# Bootstrap script for /opt/polymarket on Hetzner.
# Idempotent: safe to re-run.
#
# Prerequisites:
#   - You are root on the Hetzner box.
#   - /opt/polymarket/app/ contains the git checkout (this script lives at app/deploy/install.sh)
#   - Trading services (devantsa-*) are running and healthy.

set -euo pipefail

POLY_ROOT="/opt/polymarket"
APP_DIR="${POLY_ROOT}/app"
VENV_DIR="${POLY_ROOT}/venv"
DATA_DIR="${POLY_ROOT}/data"
LOGS_DIR="${POLY_ROOT}/logs"

echo ">>> Pre-flight: trading services must be healthy"
for svc in devantsa-loop devantsa-loop-acct2 devantsa-liq; do
    if ! systemctl is-active --quiet "$svc"; then
        echo "ABORT: $svc is not active. Resolve trading first." >&2
        exit 1
    fi
done
echo "    trading OK"

echo ">>> Ensuring directory layout"
mkdir -p "$DATA_DIR" "$LOGS_DIR"

echo ">>> Locating Python 3.11+"
PYTHON_BIN=""
for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        ver=$("$candidate" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
            PYTHON_BIN="$candidate"
            echo "    using $candidate ($ver)"
            break
        fi
    fi
done
if [ -z "$PYTHON_BIN" ]; then
    echo "ABORT: no Python 3.11+ found on PATH. Install python3.11 or newer." >&2
    exit 1
fi

echo ">>> Creating venv at $VENV_DIR (idempotent)"
if [ ! -x "$VENV_DIR/bin/python" ]; then
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

echo ">>> Upgrading pip + installing polywhale"
"$VENV_DIR/bin/pip" install --upgrade pip wheel setuptools
"$VENV_DIR/bin/pip" install -e "$APP_DIR"

echo ">>> Verifying polywhale binary"
if ! "$VENV_DIR/bin/polywhale" --help >/dev/null; then
    echo "ABORT: polywhale binary failed --help check" >&2
    exit 1
fi

echo ">>> Setting up .env (placeholder if missing)"
ENV_FILE="$APP_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" <<EOF
# Fill these in before running smoke test.
ODDS_API_KEY=your_odds_api_key_here
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=your_telegram_chat_id_here
POLYWHALE_DB_PATH=${DATA_DIR}/polywhale.sqlite
POLYWHALE_LOG_LEVEL=INFO
# (sportsbook setting; not used by polywhale)
EOF
    chmod 600 "$ENV_FILE"
    echo "    WROTE PLACEHOLDER at $ENV_FILE"
    echo "    EDIT IT NOW before running anything that needs Telegram or Odds API."
else
    echo "    .env already exists, leaving as-is"
fi

echo ">>> Running schema migrations"
cd "$APP_DIR"
"$VENV_DIR/bin/polywhale" migrate

echo ">>> Final trading health check"
for svc in devantsa-loop devantsa-loop-acct2 devantsa-liq; do
    if ! systemctl is-active --quiet "$svc"; then
        echo "ABORT: $svc went unhealthy during install" >&2
        exit 1
    fi
done

echo ""
echo "==================================================="
echo "  Install complete."
echo "  Next: edit $ENV_FILE with your real secrets,"
echo "  then proceed to Phase 2 (install systemd units)."
echo "==================================================="
