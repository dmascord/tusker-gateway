# CLI adapters invoke the provider CLIs as subprocesses. Install pinned npm
# packages in a Node stage, then copy the runtime and packages into the
# Python-based gateway image.
FROM node:22-bookworm-slim AS cli-tools

ARG CLAUDE_CODE_CLI_VERSION=2.1.281
ARG OPENCODE_CLI_VERSION=2.0.15
ARG KILO_CLI_VERSION=7.7.9

RUN npm install --global --no-audit --no-fund \
      "@anthropic-ai/claude-code@${CLAUDE_CODE_CLI_VERSION}" \
      "@opencode/cli@${OPENCODE_CLI_VERSION}" \
      "@kilocode/cli@${KILO_CLI_VERSION}" \
 && cli_arch="$(node -p 'process.arch')" \
 && mkdir -p /opt/provider-cli \
 && cp "/usr/local/lib/node_modules/@anthropic-ai/claude-code/node_modules/@anthropic-ai/claude-code-linux-${cli_arch}/claude" /opt/provider-cli/claude \
 && cp "/usr/local/lib/node_modules/@opencode/cli/node_modules/@opencode/cli-linux-${cli_arch}/bin/opencode" /opt/provider-cli/opencode \
 && cp "/usr/local/lib/node_modules/@kilocode/cli/node_modules/@kilocode/cli-linux-${cli_arch}/bin/kilo" /opt/provider-cli/kilo \
 && chmod 0755 /opt/provider-cli/* \
 && /opt/provider-cli/claude --version \
 && /opt/provider-cli/opencode --version \
 && /opt/provider-cli/kilo --version

FROM python:3.11-slim

LABEL org.opencontainers.image.title="tusker-gateway" \
       org.opencontainers.image.description="Tusker OpenAI-compatible gateway" \
       org.opencontainers.image.source="https://github.com/dmascord/tusker-gateway"

COPY --from=cli-tools /opt/provider-cli/ /usr/local/bin/
# Keep build-time Python imports from filling image layers with bytecode. The
# runtime sets this too, but the model prewarm below imports a large package
# tree before the final runtime environment is declared.
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /opt/tusker-gateway

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libffi-dev libssl-dev curl \
    && rm -rf /var/lib/apt/lists/*


# Install PyTorch CPU-only first to avoid pulling CUDA (5+ GB).
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
# Dependency layer: every heavy install happens before any gateway source is
# copied, so a source-only change reuses this layer instead of re-downloading
# torch/chromadb. Keep these pins in sync with pyproject.toml ([project]
# dependencies plus the semantic-cache extra).
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir \
      "aiohttp>=3.9,<4" \
      "psycopg[binary,pool]>=3.2,<4" \
      chromadb==1.5.9 \
      sentence-transformers==6.0.0

# Bake the pinned CPU embedding model into the image.  Runtime startup is
# offline by default, so a Hugging Face outage cannot delay or change the
# model used for cache keys.  The cache directory is made readable by the
# unprivileged runtime user below.
ARG TUSKER_SEMANTIC_CACHE_MODEL_REVISION=1110a243fdf4706b3f48f1d95db1a4f5529b4d41
ENV HF_HOME=/opt/huggingface \
    HF_HUB_DISABLE_TELEMETRY=1 \
    TUSKER_SEMANTIC_CACHE_MODEL_REVISION=${TUSKER_SEMANTIC_CACHE_MODEL_REVISION}
RUN mkdir -p /opt/huggingface \
 && python -c "import os; from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2', device='cpu', revision=os.environ['TUSKER_SEMANTIC_CACHE_MODEL_REVISION'])" \
 && chown -R nobody:nogroup /opt/huggingface

# pip and the model prewarm can still emit bytecode despite the environment
# guard above. Remove it before the final image commit so repeated Buildah
# builds do not retain a needless layer and exhaust the visor root volume.
RUN find /opt/tusker-gateway /usr/local/lib/python3.11 \
      -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true \
 && find /opt/tusker-gateway /usr/local/lib/python3.11 \
      -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete 2>/dev/null || true
COPY pyproject.toml ./
COPY tusker_gateway/ ./tusker_gateway/
# Strip any stale __pycache__ from build host — otherwise modules load
# from .pyc files and ignore source updates.
RUN find /opt/tusker-gateway -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
COPY README.md ./
# Install the gateway package itself without re-resolving dependencies; the
# dependency layer above already provides them.
RUN pip install --no-cache-dir --no-deps .

# CLI adapters spawn provider CLIs, which spawn servers and workers of their
# own. Those outlive their parent, are reparented to PID 1, and accumulate as
# zombies because PID 1 never reaps them (7 observed live on 2026-09-28). Run a
# real init so orphans are reaped. Installed after the dependency layers so the
# torch/chromadb/model cache above is not invalidated by this change.
RUN apt-get update && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/*

# Persistent data (quality DB, cooldowns, OAuth pool)
RUN mkdir -p /home/tusker/.hermes && chown -R nobody:nogroup /home/tusker
ENV HOME=/home/tusker \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DISABLE_AUTOUPDATER=1

# Per-build revision baked in only at the very end so it doesn't invalidate
# any of the cached dependency/model layers above.
ARG TUSKER_COMMIT=unknown
# Stamp the revision into a layer file before the ENV: buildah keys the ENV
# layer cache on the literal instruction, not the expanded ARG value, so an
# ENV-only stamp bakes the first build's commit forever (observed on buildah
# 2026-09). A RUN whose file content changes per commit forces the cache miss.
RUN printf "%s" "${TUSKER_COMMIT}" > /opt/tusker-gateway/.commit \
 && chown nobody:nogroup /opt/tusker-gateway/.commit
ENV TUSKER_COMMIT=${TUSKER_COMMIT}

USER nobody

# No HEALTHCHECK here: the image is pushed in OCI format, where buildah drops the
# directive ("HEALTHCHECK is not supported for OCI image format"), and the k8s
# startup/readiness/liveness probes are what actually gate a pod.

ENTRYPOINT ["tini", "--", "python", "-m", "tusker_gateway"]
