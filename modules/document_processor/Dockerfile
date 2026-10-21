# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
FROM mcr.microsoft.com/mirror/docker/library/ubuntu:24.04

# Install runtime dependencies for PDF processing
RUN apt-get update && apt-get install -y \
    ca-certificates \
    poppler-utils \
    tesseract-ocr \
    curl \
    procps \
    && rm -rf /var/lib/apt/lists/*

ENV UV_PYTHON_INSTALL_DIR=/opt/python
RUN curl -LsSf https://astral.sh/uv/0.13.1/install.sh -o /tmp/uv-install.sh \
    && UV_INSTALL_DIR=/usr/local/bin sh /tmp/uv-install.sh \
    && rm /tmp/uv-install.sh \
    && uv python install 3.14
ENV UV_PYTHON=3.14
ENV UV_PYTHON_DOWNLOADS=never

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
COPY --chown=app:app pyproject.toml uv.lock* README.md ./

# Install dependencies - UV handles virtual environment creation
RUN uv sync --locked --no-install-project --no-editable

# Copy application code maintaining structure
COPY --chown=app:app src/ ./src/

# Install the project itself in non-editable mode
RUN uv sync --locked --no-editable

# Set PATH to include virtual environment
ENV PATH="/app/.venv/bin:$PATH"

# Entry point for inference server
CMD ["python", "-m", "src.document_processor.app"]
