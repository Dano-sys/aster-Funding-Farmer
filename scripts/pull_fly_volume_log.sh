#!/usr/bin/env bash
# Pull the persisted app logfile from the Fly volume to a local file.
#
# Uses `fly ssh console` to avoid sftp setup friction.
#
# Examples:
#   bash scripts/pull_fly_volume_log.sh --app aster-funding-farmer --lines 400
#   bash scripts/pull_fly_volume_log.sh --lines 800   # uses app from fly.toml when possible
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

APP="${FLY_APP_NAME:-}"
LINES="500"
OUT_DIR="fly-logs"
REMOTE_LOG="${REMOTE_FUNDING_FARMER_LOG:-/data/funding_farmer.log}"

usage() {
  cat <<'EOF'
Usage: pull_fly_volume_log.sh [--app APP] [--lines N] [--out-dir DIR] [--remote-path PATH]

Options:
  --app APP           Fly app name (or set FLY_APP_NAME env var)
  --lines N           How many lines to fetch from the end (default: 500)
  --out-dir DIR       Local output directory (default: fly-logs)
  --remote-path PATH  Remote log path (default: /data/funding_farmer.log)

Notes:
  - This pulls from the VM volume log, not the `fly logs` platform stream.
  - Requires: flyctl authenticated and `fly ssh` access to the app.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --app)
      APP="${2:-}"; shift 2;;
    --lines)
      LINES="${2:-}"; shift 2;;
    --out-dir)
      OUT_DIR="${2:-}"; shift 2;;
    --remote-path)
      REMOTE_LOG="${2:-}"; shift 2;;
    -h|--help)
      usage; exit 0;;
    *)
      echo "Unknown arg: $1" >&2
      usage
      exit 2;;
  esac
done

if [ -z "$APP" ]; then
  if [ -f fly.toml ]; then
    APP="$(python3 - <<'PY'
import re, pathlib
p = pathlib.Path("fly.toml")
txt = p.read_text(encoding="utf-8", errors="replace")
m = re.search(r'(?m)^app\s*=\s*"(.*?)"\s*$', txt)
print(m.group(1) if m else "")
PY
)"
  fi
fi

if [ -z "$APP" ]; then
  echo "Missing Fly app name. Pass --app or set FLY_APP_NAME." >&2
  exit 2
fi

mkdir -p "$OUT_DIR"

ts="$(date -u +"%Y%m%d-%H%M%S")"
out="$OUT_DIR/funding_farmer-${APP}-${ts}.log"

echo "Pulling last ${LINES} lines from ${APP}:${REMOTE_LOG} -> ${out}" >&2
fly ssh console -a "$APP" -C "tail -n ${LINES} \"${REMOTE_LOG}\"" >"$out"

echo "$out"
