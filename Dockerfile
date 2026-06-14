# ─────────────────────────────────────────────────────────────
# Stage 1: build — compile C extensions in full image
# ─────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --prefix=/install --no-cache-dir -r requirements.txt

# ─────────────────────────────────────────────────────────────
# Stage 2: runtime — minimal image (~85 MB)
# ─────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

WORKDIR /app

# Non-root user for security
RUN useradd --create-home --shell /bin/bash botuser

# Copy compiled packages from builder
COPY --from=builder /install /usr/local

# Copy source
COPY apex_scalper/ ./apex_scalper/

# Logs directory (mounted as volume in docker-compose)
RUN mkdir -p /app/logs && chown botuser:botuser /app/logs

USER botuser

# Health check: ensure the package is importable (catches broken envs)
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import apex_scalper.config" || exit 1

CMD ["python", "-m", "apex_scalper.main"]
