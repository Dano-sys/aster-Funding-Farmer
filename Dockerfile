FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# git: optional CODE_REVIEW_INCLUDE_GIT_DIFF in code_review_scheduler.py (Fly image otherwise has no git).
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x docker-entrypoint.sh

# Main bot: funding_farmer.py (set DRY_RUN=true via fly secrets for paper / no orders).
# Optional sidecars (see docker-entrypoint.sh):
#   ALERT_WATCHER_ENABLED + WEBHOOK_URL or Telegram
#   CLAUDE_ADVISOR_LOOP_ON_FLY + CLAUDE_ADVISOR_ENABLED + ANTHROPIC_API_KEY → claude_advisor loop → /data/claude_loop.log
# For minimal live staging locally: run_small_staged.py --live-small ... --no-clean-slate
ENTRYPOINT ["./docker-entrypoint.sh"]
