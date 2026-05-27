#!/bin/bash
# Poly-side health audit. Run anytime to verify the deploy is healthy.
# Exits non-zero if any check fails (useful for cron-based alerting).

set -u

POLY_ROOT="/opt/polymarket"
LOGS_DIR="${POLY_ROOT}/logs"
UNITS=(poly-watch whale-fast poly-arbs poly-paper-settle)
FAIL=0

echo "============================================================"
echo "  Polymarket bot health check  $(date -u +'%Y-%m-%d %H:%M:%S UTC')"
echo "============================================================"

# 1) Trading services first - poly should never affect them
echo ""
echo "[1/4] Trading services (independent check):"
for svc in devantsa-loop devantsa-loop-acct2 devantsa-liq; do
    status=$(systemctl is-active "$svc" 2>/dev/null || echo "UNKNOWN")
    if [ "$status" = "active" ]; then
        printf "      OK   %s\n" "$svc"
    else
        printf "      WARN %s = %s (not poly's problem, but flagged)\n" "$svc" "$status"
    fi
done

# 2) Poly timers
echo ""
echo "[2/4] Poly timers (all should be enabled + scheduled):"
for unit in "${UNITS[@]}"; do
    enabled=$(systemctl is-enabled "${unit}.timer" 2>/dev/null || echo "missing")
    active=$(systemctl is-active "${unit}.timer" 2>/dev/null || echo "missing")
    if [ "$enabled" = "enabled" ] && [ "$active" = "active" ]; then
        next=$(systemctl list-timers "${unit}.timer" --no-pager 2>/dev/null | awk 'NR==2 {print $1, $2, $3}' || echo "unknown")
        printf "      OK   %-22s next: %s\n" "$unit" "$next"
    else
        printf "      FAIL %-22s enabled=%s active=%s\n" "$unit" "$enabled" "$active"
        FAIL=1
    fi
done

# 3) Log freshness (last successful run was <2h ago)
echo ""
echo "[3/4] Log freshness (last write < 2 hours ago):"
NOW=$(date +%s)
for unit in "${UNITS[@]}"; do
    log_file="${LOGS_DIR}/${unit}.log"
    if [ ! -f "$log_file" ]; then
        printf "      WARN %-22s no log file yet (first run hasn't happened?)\n" "$unit"
        continue
    fi
    last_mtime=$(stat -c %Y "$log_file")
    age=$((NOW - last_mtime))
    # Thresholds match each timer's cadence with slack.
    if [ "$unit" = "poly-paper-settle" ]; then
        threshold=90000  # daily timer, allow 25h
    elif [ "$unit" = "whale-fast" ]; then
        threshold=300    # 60s cadence, allow 5min
    else
        threshold=7200   # 2h for poly-watch / poly-arbs
    fi
    if [ "$age" -lt "$threshold" ]; then
        printf "      OK   %-22s %ss old\n" "$unit" "$age"
    else
        printf "      FAIL %-22s %ss old (threshold %ss)\n" "$unit" "$age" "$threshold"
        FAIL=1
    fi
done

# 4) Recent errors in logs (last 100 lines, no ERROR or Traceback)
echo ""
echo "[4/4] Recent errors (last 100 lines per log):"
for unit in "${UNITS[@]}"; do
    log_file="${LOGS_DIR}/${unit}.log"
    if [ ! -f "$log_file" ]; then
        continue
    fi
    errors=$(tail -100 "$log_file" 2>/dev/null | grep -cE 'ERROR|Traceback|CRITICAL' || true)
    if [ "$errors" -eq 0 ]; then
        printf "      OK   %-22s clean\n" "$unit"
    else
        printf "      WARN %-22s %d error-like lines (review: tail -100 %s)\n" "$unit" "$errors" "$log_file"
        # WARN not FAIL - some errors are transient (network blips). FAIL is for missing data.
    fi
done

echo ""
echo "============================================================"
if [ "$FAIL" -eq 0 ]; then
    echo "  RESULT: HEALTHY"
    exit 0
else
    echo "  RESULT: FAILED  (see FAIL lines above)"
    exit 1
fi
