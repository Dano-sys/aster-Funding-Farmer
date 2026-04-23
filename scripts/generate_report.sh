#!/usr/bin/env bash
# One command: generate a local Markdown report from the Fly volume log.
#
# Reads secrets from ./.env when present (same pattern as scripts/claude_advisor_loop.sh).
#
# Examples:
#   bash scripts/generate_report.sh
#   REPORT_LINES=800 bash scripts/generate_report.sh --app aster-funding-farmer
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

LINES="${REPORT_LINES:-500}"

exec bash scripts/claude_fly_log_report.sh --lines "${LINES}" "$@"
