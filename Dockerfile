# ═══════════════════════════════════════════════════════
# KARA Bot — Dockerfile
# Build: docker build -t kara-bot:latest .
# Run:   docker run -e KARA_MODE=paper --env-file .env kara-bot:latest
# ═══════════════════════════════════════════════════════

FROM python:3.12-slim as base

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /app/storage

# Copy requirements
COPY requirements.txt .

# Build stage
FROM base as builder
RUN pip install --user --no-cache-dir -r requirements.txt

# Runtime stage
FROM base as runtime

# Copy installed packages from builder
COPY --from=builder /root/.local /root/.local

# Copy application code
COPY . .

# Add local pip packages to PATH
ENV PATH=/root/.local/bin:$PATH \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Health check
HEALTHCHECK --interval=60s --timeout=15s --start-period=10s --retries=3 \
    CMD python -c "import requests, os; port=os.getenv('PORT', '8888'); requests.get(f'http://localhost:{port}/api/health')" || exit 1

# Default: paper mode
ENV KARA_MODE=paper

# Expose dynamic port (Railway uses this)
EXPOSE 8888

# Run bot
CMD ["python", "main.py"]
