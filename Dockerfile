# Bellwether - single container running both the monitor and the Pond agent.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so code edits do not invalidate the layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# SQLite lives here. Mount a volume (or set DATABASE_URL to Postgres) so state
# survives restarts - without it the bot forgets and re-alerts.
VOLUME ["/data"]
ENV DATABASE_URL=""

EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
  CMD python -c "import httpx,sys; sys.exit(0 if httpx.get('http://localhost:8000/healthz', timeout=8).status_code==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
