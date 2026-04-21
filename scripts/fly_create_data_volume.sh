#!/usr/bin/env bash
# Create the Fly volume expected by fly.toml [mounts] source = "data".
# Safe to run once per app/region; fails if a volume named "data" already exists.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
APP="${FLY_APP_NAME:-aster-funding-farmer}"
REGION="${FLY_PRIMARY_REGION:-fra}"
echo "Creating volume 'data' on app=$APP region=$REGION (1GB) ..."
fly volumes create data --region "$REGION" --size 1 -a "$APP"
echo "Done. Retry: fly deploy -a $APP"
