# Sync poller image (runs on the Pi; arm64 + amd64 via python:slim).
# Build context is the repo root:  docker compose ... build sync
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backfill/ backfill/
COPY sync/ sync/

CMD ["python", "-u", "-m", "sync.poller"]
