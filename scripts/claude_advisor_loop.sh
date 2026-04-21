#!/usr/bin/env bash
# Optional: periodic claude_advisor runs while the farmer is running (separate terminal).
# Requires: pip install anthropic; ANTHROPIC_API_KEY and CLAUDE_ADVISOR_ENABLED=true in .env
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi
export CLAUDE_ADVISOR_ENABLED="${CLAUDE_ADVISOR_ENABLED:-true}"
export CLAUDE_ADVISOR_MIN_INTERVAL_SEC="${CLAUDE_ADVISOR_MIN_INTERVAL_SEC:-0}"
INTERVAL_SEC="${CLAUDE_ADVISOR_LOOP_SLEEP_SEC:-180}"
echo "Claude advisor loop: every ${INTERVAL_SEC}s (Ctrl+C to stop). CLAUDE_ADVISOR_ENABLED=${CLAUDE_ADVISOR_ENABLED}"
while true; do
  python3 claude_advisor.py run || true
  sleep "${INTERVAL_SEC}"
done
