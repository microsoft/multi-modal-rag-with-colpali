# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
#
# ColPali Inference shim image (CPU-only FastAPI front-end).
# The vLLM sidecar is built separately from Dockerfile.vllm.

# UV (Python package manager) from the official ghcr image.
FROM ghcr.io/astral-sh/uv:latest AS uvimage

# Azure Linux Python base image.
FROM mcr.microsoft.com/azurelinux/base/python:3.12
COPY --from=uvimage /uv /uvx /bin/

ARG BUILD_NUMBER

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONHASHSEED=random
ENV PYTHONIOENCODING="utf-8"
ENV PYTHONUTF8=1
ENV PYTHONPATH=/app/src
ENV PORT=8000

# UV tuning.
ENV UV_LINK_MODE=copy
ENV UV_COMPILE_BYTECODE=1
ENV UV_CACHE_DIR="/tmp/uv-cache"

# Minimal runtime deps. curl is used by the HEALTHCHECK; ca-certificates for
# outbound HTTPS (model download, telemetry).
RUN tdnf install -y \
    ca-certificates curl \
    && tdnf clean all

WORKDIR /app

# Copy dependency files first for better layer caching.
COPY pyproject.toml uv.lock README.md ./

# Install dependencies (no private sources — all from public PyPI).
RUN uv sync --locked --no-install-project --no-editable --no-dev

# Copy application code.
COPY src/ ./src/

# Install the project itself (non-editable so /app/src isn't writable).
RUN uv sync --locked --no-editable --no-dev


# Put the venv on PATH.
ENV PATH="/app/.venv/bin:$PATH"

# HuggingFace cache for processor files (model weights live on the PVC at
# MODEL_DIRECTORY_PATH, populated by the init container).
ENV HF_HOME="/tmp/hf_cache"

# Number of uvicorn worker processes. The shim is CPU-bound (tokenize, image
# decode, pooling math); multiple workers parallelise across cores. vLLM
# remains the single GPU consumer and owns continuous batching.
ENV UVICORN_WORKERS=4

# Embed BUILD_NUMBER for the /health build-id contract used by post-deploy checks.
RUN echo "BUILD_NUMBER=${BUILD_NUMBER:-localbuild.$(date +%Y%m%d%H%M)}" > .env

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# The Python entrypoint exec's uvicorn with --workers when invoked without
# the "download" arg (init container uses "download" to populate the PVC).
CMD ["python", "-m", "src.colpali_inference.app"]
