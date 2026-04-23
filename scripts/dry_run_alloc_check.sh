#!/usr/bin/env bash
# Local dry run with Fly-like *sizing* so [sizing] lines show 7-pool / fair-leg math.
# (Your .env may have MAX_POSITIONS=2 and WALLET_MAX_USD=150 — that hides real allocation.)
# This writes repo-root `env` which config loads *after* `.env` and overrides.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
DRY_MAX_CYCLES="${DRY_MAX_CYCLES:-2}"
cat >"$ROOT/env" <<'EOF'
DRY_RUN=true
DRY_RUN_SIMULATED_MARGIN_USD=0
POLL_INTERVAL_SEC=5
RISK_POLL_INTERVAL_SEC=5
# --- mirror production allocation intent (override .env) ---
MAX_POSITIONS=7
MAX_POSITIONS_AUTO=false
RESERVE_SLOT_FOR_NEW_POOLS=false
WALLET_MAX_USD=0
WALLET_MIN_USD=20
WALLET_DEPLOY_PCT=0.80
ALLOCATION_MODE=rank_weighted
RANK_TOP_PCT=0.25
MAX_SINGLE_PCT=0.30
LEVERAGE=3
MIN_FUNDING_RATE=0.0001
MIN_QUOTE_VOLUME_24H=5000000
# Must match production or you can get both SOL + SOL USD perps in paper (unlike Fly)
CORR_GROUPS=BTCUSDT|WBTCUSDT,ETHUSDT|STETHUSDT|WETHUSDT,SOLUSDT|SOLUSD1
DELTA_NEUTRAL=false
EOF
echo "Wrote $ROOT/env — running ${DRY_MAX_CYCLES} DRY cycle(s). Watch for [sizing] lines (pools, fair_leg, per_leg_cap, leg 1..N)."
python3 "$ROOT/funding_farmer.py" --max-cycles "$DRY_MAX_CYCLES"
EX=$?
rm -f "$ROOT/env"
if [[ $EX -eq 0 ]]; then
  echo "OK: done (removed $ROOT/env)."
else
  echo "Exit $EX — env file removed."
fi
exit "$EX"
