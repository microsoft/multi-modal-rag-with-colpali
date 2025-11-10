"""
ColQwen2 Inference Service

FastAPI inference server that serves embeddings via REST API.
Downloads the model automatically on first startup if not present locally.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from .inference import generate_embeddings
from .logging import configure_telemetry, trace_operation
from .models import EmbedHealthResponse, EmbedRequest, EmbedResponse

# Configure telemetry on module import
configure_telemetry()

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan handler for model initialization."""
    logger.info("Starting ColQwen2 inference server...")

    # Start model initialization in background - don't block app startup
    from .inference import init_model

    # Start model loading asynchronously
    async def load_model_async():
        try:
            logger.info("Initializing ColQwen2 model (includes download if needed)...")
            await asyncio.to_thread(init_model)
            logger.info("ColQwen2 model initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize model: {e}")
            # Don't raise - let health checks handle the failed state

    # Start model loading in background
    asyncio.create_task(load_model_async())

    try:
        yield
    finally:
        logger.info("Shutting down ColQwen2 inference server...")


# Create FastAPI app directly
app = FastAPI(
    title="ColQwen2 Inference Service",
    description="FastAPI service for ColQwen2 document understanding and embedding generation",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", response_model=EmbedHealthResponse)
async def health_check():
    """Health check endpoint. Returns 503 if model is not ready, 200 if ready."""
    try:
        # Import here to avoid circular imports
        from .inference import device, model, processor

        # Check if model components are loaded
        model_loaded = all(
            [model is not None, processor is not None, device is not None]
        )

        if not model_loaded:
            # Raise 503 Service Unavailable if model is still loading
            raise HTTPException(
                status_code=503,
                detail=EmbedHealthResponse(
                    status="initializing",
                    model_loaded=False,
                    model_info=None,
                    message="Model is still loading, please wait...",
                ).model_dump(),
            )

        model_info = {
            "model_name": "vidore/colqwen2-v1.0-hf",
            "device": str(device),
            "architecture": "ColQwen2",
        }

        return EmbedHealthResponse(
            status="healthy",
            model_loaded=True,
            model_info=model_info,
        )
    except HTTPException:
        # Re-raise HTTPExceptions to preserve status codes
        raise
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        # Return 503 for any errors during initialization
        raise HTTPException(
            status_code=503,
            detail=EmbedHealthResponse(
                status="unhealthy",
                model_loaded=False,
                model_info=None,
                error=str(e),
            ).model_dump(),
        )


@app.post("/embeddings", response_model=EmbedResponse)
async def generate_embeddings_endpoint(request: EmbedRequest):
    """Main embedding endpoint - handles both text and image embeddings."""
    try:
        # Validate input - at least one of texts or images must be provided
        if not request.texts and not request.images:
            raise HTTPException(
                status_code=400,
                detail="Either 'texts' or 'images' must be provided",
            )

        if request.texts and request.images:
            raise HTTPException(
                status_code=400,
                detail="Cannot process both 'texts' and 'images' in the same request",
            )

        # Run inference in thread pool to avoid blocking event loop
        result = await asyncio.to_thread(generate_embeddings, request)

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Embedding generation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/", response_model=EmbedHealthResponse)
async def root():
    """Root endpoint - returns same as health check for convenience."""
    return await health_check()


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Global exception handler."""
    logger.error(f"Unhandled exception: {exc}")
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "detail": str(exc)},
    )


def server_mode():
    """Run FastAPI inference server mode."""
    logger.info("Starting ColQwen2 inference server mode...")

    import uvicorn

    # Configuration from environment variables
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8080"))
    log_level = os.getenv("LOG_LEVEL", "info").lower()

    logger.info(f"Starting ColQwen2 inference server on {host}:{port}")
    logger.info(f"Log level: {log_level}")
    logger.info(
        "Using single worker (required for model loading with lifespan handlers)"
    )

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level,
        workers=1,  # Single worker required for lifespan compatibility and model sharing
        reload=False,
    )


@trace_operation("model_download")
def download_mode():
    """Download mode for init container - downloads model then exits."""
    from .inference import _download_model_if_needed

    logger.info("ColQwen2 container starting in download mode")

    # Get paths from environment variables
    model_directory = os.getenv("MODEL_DIRECTORY_PATH", "/tmp/model-directory")
    model_name = os.getenv("MODEL_ID", "vidore/colqwen2-v1.0-hf")

    logger.info("Init container: Starting model download...")
    logger.info("Model ID: %s", model_name)
    logger.info("Model directory: %s", model_directory)

    try:
        # Use the existing download function
        _download_model_if_needed(model_directory)
        logger.info("Init container: Model download completed successfully")
    except Exception as e:
        logger.error(f"Model download failed: {e}")
        raise


def main():
    """Main entry point - checks for download mode or runs as inference server."""
    import sys

    # Check for download mode
    if len(sys.argv) > 1 and sys.argv[1] == "download":
        download_mode()
    else:
        logger.info("ColQwen2 container starting as inference server")
        server_mode()


if __name__ == "__main__":
    main()
