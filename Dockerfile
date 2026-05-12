
#  Brand Bade Poster — Dockerfile
#  Multi-stage build:
#    Stage 1 (builder) — compiles Python deps in isolation
#    Stage 2 (runtime) — lean, non-root production image


# Build args (injected by CI for traceability)
ARG PYTHON_VERSION=3.11
ARG APP_VERSION=dev
ARG BUILD_DATE
ARG GIT_SHA

#  Stage 1: Builder 
FROM python:${PYTHON_VERSION}-slim AS builder

WORKDIR /build

# Prevent Python from writing .pyc files and buffering stdout
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build tools needed for faster-whisper and Google API packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    pkg-config \
    libavformat-dev \
    libavcodec-dev \
    libavdevice-dev \
    libavutil-dev \
    libavfilter-dev \
    libswscale-dev \
    libswresample-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Install all Python dependencies into a dedicated prefix so they copy cleanly
RUN pip install --prefix=/install --no-cache-dir -r requirements.txt


#  Stage 2: Runtime 
FROM python:${PYTHON_VERSION}-slim

# OCI-compliant image labels (replaces deprecated LABEL key=value pairs)
LABEL org.opencontainers.image.title="Brand Bade Poster" \
      org.opencontainers.image.description="AI video automation for eMigr8" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.vendor="Bincom / eMigr8" \
      org.opencontainers.image.licenses="Proprietary"

# Python runtime flags  set once here so every process inherits them
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_NO_CACHE_DIR=1

# Runtime system dependencies only  no build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder stage
COPY --from=builder /install /usr/local

# Create a dedicated non-root user for the application
RUN useradd --system --uid 1000 --no-create-home --shell /sbin/nologin appuser \
    && mkdir -p /home/appuser/.cache \
    && chown appuser:appuser /home/appuser/.cache

WORKDIR /app

# Create runtime directories with correct ownership in one layer
RUN mkdir -p \
    uploads \
    output/clips \
    output/thumbnails \
    output/store \
    overlays \
    logs \
    gdrive_inbox \
    watch_inbox \
    watch_processed \
    static \
    scripts \
    && chown -R appuser:appuser /app

# Copy application code (chown on COPY avoids a separate chown layer)
COPY --chown=appuser:appuser app/       ./app/
COPY --chown=appuser:appuser static/    ./static/
COPY --chown=appuser:appuser nginx/     ./nginx/
COPY --chown=appuser:appuser scripts/   ./scripts/
COPY --chown=appuser:appuser overlays/  ./overlays/
COPY --chown=appuser:appuser cli.py run.py setup_check.py ./

USER appuser

EXPOSE 8000

# Use a dedicated /health endpoint for more accurate health signalling.
# If you don't have one yet, add GET /health  {"status":"ok"} to main.py.
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Use exec form so uvicorn receives SIGTERM directly (not via shell wrapper)
CMD ["python", "run.py"]
