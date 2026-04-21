#!/bin/sh
# Fly / Docker: main process is funding_farmer.py (PID 1 after exec).
# Optional: ALERT_WATCHER_ENABLED=true with WEBHOOK_URL or Telegram → background alert_watcher.py
# Optional: CLAUDE_ADVISOR_LOOP_ON_FLY=true + CLAUDE_ADVISOR_ENABLED + ANTHROPIC_API_KEY → background claude_advisor loop
set -e
cd /app
# No-op when /data is not used; with Fly [mounts] this is the volume mount point.
mkdir -p /data 2>/dev/null || true

_aw="${ALERT_WATCHER_ENABLED:-false}"
case "$_aw" in
  true|TRUE|1|yes|YES)
    if [ -n "${WEBHOOK_URL:-}" ] || { [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; }; then
      echo "entrypoint: starting alert_watcher.py in background" >&2
      python3 alert_watcher.py &
    else
      echo "entrypoint: ALERT_WATCHER_ENABLED set but no WEBHOOK_URL or Telegram pair; skipping alert_watcher" >&2
    fi
    ;;
esac

_cloop="${CLAUDE_ADVISOR_LOOP_ON_FLY:-false}"
case "$_cloop" in
  true|TRUE|1|yes|YES)
    _cae="${CLAUDE_ADVISOR_ENABLED:-false}"
    case "$_cae" in
      true|TRUE|1|yes|YES)
        if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
          echo "entrypoint: CLAUDE_ADVISOR_LOOP_ON_FLY set but ANTHROPIC_API_KEY empty; skipping claude_advisor loop" >&2
        else
          echo "entrypoint: starting claude_advisor loop in background (log: /data/claude_loop.log)" >&2
          _csleep="${CLAUDE_ADVISOR_LOOP_SLEEP_SEC:-180}"
          nohup sh -c 'while true; do python3 claude_advisor.py run || true; sleep '"$_csleep"'; done' >>/data/claude_loop.log 2>&1 &
        fi
        ;;
      *)
        echo "entrypoint: CLAUDE_ADVISOR_LOOP_ON_FLY set but CLAUDE_ADVISOR_ENABLED not true; skipping claude_advisor loop" >&2
        ;;
    esac
    ;;
esac

exec python3 funding_farmer.py "$@"
