#!/usr/bin/env bash
# One command: generate a local Markdown report from Fly logs AND a matching plan file.
#
# Writes:
# - reports/claude-<app>-<ts>.md  (existing generator)
# - fly-logs/claude-advisor-<app>-<ts>.json  (existing generator)
# - plans/plan-<app>-<ts>.md  (new)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

report_path="$(bash scripts/generate_report.sh "$@")"
report_path="$(echo "$report_path" | tail -n 1 | tr -d '\r')"

if [ -z "$report_path" ]; then
  echo "Could not determine report path from scripts/generate_report.sh output" >&2
  exit 2
fi

# Extract timestamp suffix YYYYMMDD-HHMMSS from the report filename:
#   reports/claude-<app>-YYYYMMDD-HHMMSS.md
ts="$(python3 - <<'PY'
import os, re, sys
p = sys.argv[1]
m = re.search(r'-(\d{8}-\d{6})\.md$', os.path.basename(p))
print(m.group(1) if m else "")
PY
"$report_path")"

if [ -z "$ts" ]; then
  echo "Could not parse timestamp from report path: $report_path" >&2
  exit 2
fi

# Find the advisor JSON produced alongside the report.
advisor_json="$(ls -1 fly-logs/claude-advisor-*-"${ts}".json 2>/dev/null | head -n 1 || true)"
if [ -z "$advisor_json" ]; then
  echo "Could not find advisor JSON for ts=${ts} under fly-logs/" >&2
  exit 2
fi

app="$(python3 - <<'PY'
import os, re, sys
name = os.path.basename(sys.argv[1])
# fly-logs/claude-advisor-<app>-YYYYMMDD-HHMMSS.json
m = re.match(r'^claude-advisor-(.+)-\d{8}-\d{6}\.json$', name)
print(m.group(1) if m else "aster-funding-farmer")
PY
"$advisor_json")"

out="plans/plan-${app}-${ts}.md"

python3 scripts/advisor_json_to_plan.py --in "$advisor_json" --out "$out" --repo-root "$ROOT" >/dev/null

echo "$report_path"
echo "$out"

