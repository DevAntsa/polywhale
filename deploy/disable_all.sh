#!/bin/bash
# One-shot disable. Stops and disables all poly + whale timers and services.
# Leaves trading services (devantsa-*) completely untouched.
#
# Use cases:
# - Emergency rollback if something looks wrong
# - Temporary pause for maintenance
# - Phase 5 deploy verification

set -u

POLY_UNITS=(poly-watch poly-arbs poly-paper-settle)
WHALE_UNITS=(whale-watch whale-signals whale-fast whale-refresh)

echo ">>> Disabling poly + whale timers/services..."
for unit in "${POLY_UNITS[@]}" "${WHALE_UNITS[@]}"; do
    systemctl stop "${unit}.timer" 2>/dev/null || true
    systemctl stop "${unit}.service" 2>/dev/null || true
    systemctl disable "${unit}.timer" 2>/dev/null || true
done

echo ""
echo ">>> Poly fully disabled."
echo ""
echo ">>> Trading services (should all be 'active'):"
for svc in devantsa-loop devantsa-loop-acct2 devantsa-liq; do
    status=$(systemctl is-active "$svc" 2>/dev/null || echo "UNKNOWN")
    printf "    %-25s %s\n" "$svc" "$status"
done
echo ""
echo "If any trading service is not 'active', this is independent of poly."
echo "To re-enable poly (fast loop + weekly refresh):"
echo "    systemctl enable --now poly-watch.timer whale-fast.timer whale-refresh.timer poly-arbs.timer poly-paper-settle.timer"
