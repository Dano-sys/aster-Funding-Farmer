#!/usr/bin/env bash
# End-to-end: pull Fly volume log tail, run claude_advisor locally, emit timestamped Markdown report.
#
# Example:
#   bash scripts/claude_fly_log_report.sh --app aster-funding-farmer --lines 400
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

_dotenv_get() {
  # Read a single KEY from ./.env without sourcing it (works even if .env isn't valid shell).
  # Supports:
  #   KEY=value
  #   KEY="value"
  #   KEY='value'
  # Ignores commented lines.
  python3 - "$1" <<'PY' 2>/dev/null || true
import re, sys
key = sys.argv[1]
try:
    lines = open(".env", "r", encoding="utf-8", errors="replace").read().splitlines()
except OSError:
    print("")
    raise SystemExit(0)
pat = re.compile(rf"^\s*{re.escape(key)}\s*=\s*(.*?)\s*$")
for line in lines:
    if not line or line.lstrip().startswith("#"):
        continue
    m = pat.match(line)
    if not m:
        continue
    v = m.group(1).strip()
    if len(v) >= 2 and ((v[0] == v[-1] == '"') or (v[0] == v[-1] == "'")):
        v = v[1:-1]
    print(v)
    raise SystemExit(0)
print("")
PY
}

# Prefer already-exported env vars; fall back to reading .env for the couple keys we need.
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  _k="$(_dotenv_get ANTHROPIC_API_KEY)"
  if [ -n "${_k}" ]; then
    export ANTHROPIC_API_KEY="$_k"
  fi
fi
if [ -z "${CLAUDE_MODEL:-}" ]; then
  _m="$(_dotenv_get CLAUDE_MODEL)"
  if [ -n "${_m}" ]; then
    export CLAUDE_MODEL="$_m"
  fi
fi

# Avoid a known-dead default some envs still carry.
if [ "${CLAUDE_MODEL:-}" = "claude-3-5-haiku-20241022" ]; then
  export CLAUDE_MODEL="claude-haiku-4-5"
fi

# Give the model enough output budget to finish valid JSON.
if [ -z "${CLAUDE_ADVISOR_MAX_TOKENS:-}" ]; then
  export CLAUDE_ADVISOR_MAX_TOKENS="2048"
fi

APP="${FLY_APP_NAME:-}"
LINES="500"
LOG_MODE="${CLAUDE_ADVISOR_LOG_MODE:-tail}"
MAX_LOG_LINES="${CLAUDE_ADVISOR_MAX_LOG_LINES:-250}"
REPORTS_DIR="reports"

usage() {
  cat <<'EOF'
Usage: claude_fly_log_report.sh [--app APP] [--lines N]

Pulls the last N lines from /data/funding_farmer.log on the Fly VM, runs claude_advisor.py locally
against that downloaded file, and writes a timestamped Markdown report under ./reports/.

Env you typically set:
  ANTHROPIC_API_KEY=...           (required unless present in ./.env)
  CLAUDE_ADVISOR_ENABLED=true     (recommended; this script sets it to true for this run)

Optional env to control token/cost:
  CLAUDE_MODEL=claude-haiku-4-5
  CLAUDE_ADVISOR_LOG_MODE=tail|errors
  CLAUDE_ADVISOR_MAX_LOG_LINES=250
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --app)
      APP="${2:-}"; shift 2;;
    --lines)
      LINES="${2:-}"; shift 2;;
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
txt = pathlib.Path("fly.toml").read_text(encoding="utf-8", errors="replace")
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

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "Missing ANTHROPIC_API_KEY in environment." >&2
  exit 2
fi

pulled_log="$(bash scripts/pull_fly_volume_log.sh --app "$APP" --lines "$LINES")"

ts="$(date -u +"%Y%m%d-%H%M%S")"
json_out="fly-logs/claude-advisor-${APP}-${ts}.json"
md_out="${REPORTS_DIR}/claude-${APP}-${ts}.md"

echo "Running local claude_advisor.py using FUNDING_FARMER_LOG=${pulled_log}" >&2

set +e
(
  export CLAUDE_ADVISOR_ENABLED="true"
  export FUNDING_FARMER_LOG="${pulled_log}"
  export CLAUDE_ADVISOR_LOG_MODE="${LOG_MODE}"
  export CLAUDE_ADVISOR_MAX_LOG_LINES="${MAX_LOG_LINES}"
  python3 claude_advisor.py run
) | tee "${json_out}"
rc="${PIPESTATUS[0]:-1}"
set -e

if [ "$rc" -ne 0 ]; then
  echo "claude_advisor.py failed (exit ${rc}). JSON output (if any) is at: ${json_out}" >&2
  exit "$rc"
fi

python3 scripts/claude_advisor_json_to_md.py --in "${json_out}" --out "${md_out}" --model "${CLAUDE_MODEL:-}" --source "${pulled_log}" >/dev/null
echo "${md_out}"
