# Local Claude analysis of Fly volume logs

This repo writes an **app-managed logfile** to the Fly volume at `/data/funding_farmer.log` (configured by `fly.toml`).
This doc shows how to pull that logfile locally and generate a **timestamped Markdown report** using `claude_advisor.py`.

## Prereqs

- `flyctl` installed and authenticated
- You can SSH to the app (e.g. `fly ssh console -a aster-funding-farmer`)
- Python deps installed locally:

```bash
python3 -m pip install -r requirements.txt
```

- `ANTHROPIC_API_KEY` available locally (recommended: put it in `./.env` so you do not need to export it)

## One command: pull + analyze + write Markdown

Recommended (reads `./.env` automatically):

```bash
bash scripts/generate_report.sh
```

Equivalent (same behavior; also reads `./.env` automatically):

```bash
bash scripts/claude_fly_log_report.sh --lines 500
```

If you prefer exporting the key for a one-off run:

```bash
ANTHROPIC_API_KEY='...your key...' bash scripts/claude_fly_log_report.sh --lines 500
```

This writes:

- a pulled log snapshot under `fly-logs/`
- the raw advisor JSON under `fly-logs/`
- a Markdown report under `reports/` like `reports/claude-aster-funding-farmer-YYYYMMDD-HHMMSS.md`

Both `fly-logs/` and `reports/` are gitignored.

## Optional: daily report inside the running bot (Fly)

If you want the long-running `funding_farmer.py` process to write a daily Markdown report automatically, enable:

- `CLAUDE_ADVISOR_DAILY_REPORT_ENABLED=true`
- `CLAUDE_ADVISOR_ENABLED=true`
- `ANTHROPIC_API_KEY=...`

On Fly, prefer writing under the mounted volume:

- `CLAUDE_ADVISOR_DAILY_REPORT_DIR=/data/reports`
- `CLAUDE_ADVISOR_DAILY_REPORT_LAST_RUN_FILE=/data/.claude_advisor_daily_report_last_run`

See `.env.example` for the full knob list.

## Cheaper runs (recommended defaults)

Only send error-like lines + fewer total lines:

```bash
CLAUDE_ADVISOR_LOG_MODE=errors \
CLAUDE_ADVISOR_MAX_LOG_LINES=200 \
bash scripts/claude_fly_log_report.sh --lines 2000
```

## Pull the volume log only (no Claude call)

```bash
bash scripts/pull_fly_volume_log.sh --lines 800
```

## Run Claude advisor against a specific pulled file

```bash
export CLAUDE_ADVISOR_ENABLED=true
export FUNDING_FARMER_LOG="fly-logs/funding_farmer-aster-funding-farmer-YYYYMMDD-HHMMSS.log"
python3 claude_advisor.py run > fly-logs/advisor.json

python3 scripts/claude_advisor_json_to_md.py \
  --in fly-logs/advisor.json \
  --out "reports/claude-$(date -u +%Y%m%d-%H%M%S).md" \
  --model "${CLAUDE_MODEL:-}" \
  --source "${FUNDING_FARMER_LOG}"
```

