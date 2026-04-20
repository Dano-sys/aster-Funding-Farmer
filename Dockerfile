FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Main bot on Fly: funding_farmer.py (set DRY_RUN=true via fly secrets for paper / no orders).
# For minimal live staging instead, use: run_small_staged.py --live-small ... --no-clean-slate
CMD ["python3", "funding_farmer.py"]
