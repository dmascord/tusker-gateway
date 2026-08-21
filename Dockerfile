FROM python:3.11-slim

LABEL org.opencontainers.image.title="tusker-gateway" \
       org.opencontainers.image.description="Tusker OpenAI-compatible gateway" \
       org.opencontainers.image.source="https://github.com/dmascord/tusker-gateway"

ARG TUSKER_COMMIT=unknown
ENV TUSKER_COMMIT=${TUSKER_COMMIT}
WORKDIR /opt/tusker-gateway

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libffi-dev libssl-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY tusker_gateway/ ./tusker_gateway/
# Strip any stale __pycache__ from build host — otherwise modules load
# from .pyc files and ignore source updates.
RUN find /opt/tusker-gateway -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
COPY README.md ./

# Install PyTorch CPU-only first to avoid pulling CUDA (5+ GB).
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir ".[semantic-cache]"

# Persistent data (quality DB, cooldowns, OAuth pool)
RUN mkdir -p /home/tusker/.hermes && chown -R nobody:nogroup /home/tusker
ENV HOME=/home/tusker \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER nobody

HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
    CMD curl -f http://127.0.0.1:8642/health || exit 1

ENTRYPOINT ["python", "-m", "tusker_gateway"]
