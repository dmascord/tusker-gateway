FROM python:3.11-alpine

LABEL org.opencontainers.image.title="tusker-gateway" \
      org.opencontainers.image.description="Tusker OpenAI-compatible gateway" \
      org.opencontainers.image.source="https://github.com/dmascord/tusker-gateway"

WORKDIR /opt/tusker-gateway

RUN apk add --no-cache build-base libffi-dev openssl-dev

COPY pyproject.toml ./
COPY tusker_gateway/ ./tusker_gateway/
COPY README.md ./

RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir .

# Persistent data (quality DB, cooldowns, OAuth pool)
RUN mkdir -p /home/tusker/.hermes && chown -R nobody:nogroup /home/tusker
ENV HOME=/home/tusker \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER nobody

EXPOSE 8642

HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
    CMD wget -qO- http://127.0.0.1:8642/health || exit 1

ENTRYPOINT ["python", "-m", "tusker_gateway"]
