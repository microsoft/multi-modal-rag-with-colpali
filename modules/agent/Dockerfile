# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
FROM python:3.11-slim-bookworm

# Copy UV binary from official UV image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Install minimal runtime dependencies (no Rust toolchain)
RUN apt-get update && apt-get install -y \
    curl \
    procps \
    build-essential \
    pkg-config \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Configure UV for optimal performance
ENV UV_LINK_MODE=copy
ENV UV_COMPILE_BYTECODE=1

# Create non-root user for security with home directory
RUN groupadd -r app && useradd -r -g app -m app

# Set UV cache directory to a writable location
ENV UV_CACHE_DIR="/tmp/uv-cache"

# Set working directory and change ownership
WORKDIR /app
RUN chown app:app /app

# Switch to non-root user for dependency installation
USER app

# Copy dependency files first for better layer caching
COPY --chown=app:app pyproject.toml uv.lock* ./

# Install dependencies - UV handles virtual environment creation
RUN uv sync --locked --no-install-project --no-editable --no-dev

# Copy application code
COPY --chown=app:app src/ ./src/

# Install the project itself in non-editable mode
RUN uv sync --locked --no-editable --no-dev

# Set PATH to include virtual environment
ENV PATH="/app/.venv/bin:$PATH"

# Set working directory and user
WORKDIR /app
USER app

# Server configuration
ENV HOST=0.0.0.0
ENV PORT=8080

# Expose port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Entry point for API server
CMD ["python", "-m", "src.app"]
