#!/bin/bash
# Wrapper that runs polywhale poly-arbs across our current neg-risk event watchlist.
# Update the EVENT_SLUGS array as events resolve and new ones come up.

set -euo pipefail

EVENT_SLUGS=(
    "2026-fifa-world-cup-winner-595"
    "2026-nba-champion"
    "2028-democratic-presidential-nominee"
    "2028-republican-presidential-nominee"
)

ARGS=()
for slug in "${EVENT_SLUGS[@]}"; do
    ARGS+=(--event-slug "$slug")
done

exec /opt/polymarket/venv/bin/polywhale poly-arbs "${ARGS[@]}"
