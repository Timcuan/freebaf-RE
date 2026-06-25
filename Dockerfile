# freebuff2api — unified deploy (local / VPS / container)
# Python 3.11 slim, supports SOCKS5/HTTP egress proxy via httpx[socks]
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FREEBUFF_DEPLOY_MODE=vps \
    FREEBUFF_HOST=0.0.0.0 \
    FREEBUFF_PORT=8000

WORKDIR /app

# System deps minimal
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App
COPY . .

# Health check via /healthz (no auth needed for that endpoint)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${FREEBUFF_PORT}/api/keep-warm || exit 1

EXPOSE 8000

# Use unified entry point (auto-detects mode, runs egress check)
CMD ["python", "main.py"]
