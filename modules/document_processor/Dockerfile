# Single-stage build for faster development and ACR compatibility
FROM python:3.11-slim-trixie

# Copy UV binary from official UV image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Install runtime dependencies for PDF processing
RUN apt-get update && apt-get install -y \
    poppler-utils \
    tesseract-ocr \
    curl \
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
RUN uv sync --locked --no-install-project --no-editable

# Copy application code maintaining structure
COPY --chown=app:app src/ ./src/

# Install the project itself in non-editable mode
RUN uv sync --locked --no-editable

# Set PATH to include virtual environment
ENV PATH="/app/.venv/bin:$PATH"

# Entry point for inference server
CMD ["python", "-m", "src.app"]
