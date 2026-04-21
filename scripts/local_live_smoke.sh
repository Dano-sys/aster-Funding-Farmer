#!/usr/bin/env bash
# Local small live Aster run — see .env.example "Local ~10m live smoke".
# End with Ctrl+C so funding_farmer closes opens (do NOT rely on --max-cycles in live mode).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
cat <<'EOF'

================================================================
  LOCAL LIVE SMOKE — real Aster perp orders
  - Stop with Ctrl+C: graceful shutdown closes tracked longs.
  - Do NOT use --max-cycles for live exit; it leaves perps open.
  - Default: flattens ALL Aster perps at startup (run_small_staged).
    Append --no-clean-slate to skip that flatten.
================================================================

EOF
exec python3 run_small_staged.py --live-small --live-small-budget "${LIVE_SMALL_BUDGET:-120}" --live-small-pools "${LIVE_SMALL_POOLS:-2}" "$@"
