# Dockerfile
# Multi‑stage build for Self Search API + UI

# ---------- Builder stage ----------
FROM python:3.11-slim AS builder

WORKDIR /app

# Install build dependencies (if any) – here we only need pip
COPY requirements.txt ./
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . ./

# ---------- Runtime stage ----------
FROM python:3.11-slim

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY . ./

EXPOSE 8000

# Default command runs the FastAPI server.
# 0.0.0.0 here is the container's own interface and is required for port
# publishing to work. Host exposure is decided by the publish rule: use
# "127.0.0.1:8000:8000" (as docker-compose.yml does) or
# `docker run -p 127.0.0.1:8000:8000 ...`. A bare `-p 8000:8000` exposes the
# API on every host interface. See docs/deployment.md.
CMD ["uvicorn", "src.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
