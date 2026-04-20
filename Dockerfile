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

# Continuous min-live on Fly: 3 pools, small total cap, EU-friendly region in fly.toml (fra).
# Restarts: --no-clean-slate avoids flattening all Aster perps on every boot.
# Switch back to main entrypoint only: CMD ["python3", "funding_farmer.py"]
CMD ["python3", "run_small_staged.py", "--live-small", "--live-small-budget", "120", "--live-small-pools", "3", "--no-clean-slate"]
