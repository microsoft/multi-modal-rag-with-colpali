# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""
ColQwen2 Inference Service

FastAPI inference server for visual document understanding and embedding generation.
Supports both init container mode (model download) and inference server mode.
Provides REST API endpoints for health checks and embedding generation with multiple pooling strategies.
"""

import asyncio
import logging
import os
import signal
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from .inference import ColQwen2Inference
from .logging import configure_telemetry, trace_operation
from .models import EmbedHealthResponse, EmbedRequest, EmbedResponse

# Configure telemetry on module import
configure_telemetry()

logger = logging.getLogger(__name__)

# Global inference instance
inference_service = ColQwen2Inference()
HEALTH_MODEL_NAME = "vidore/colqwen2"


class GracefulShutdown:
    """Handle graceful shutdown for ColQwen2 inference service."""

    def __init__(self):
        self.shutdown_event = asyncio.Event()
        self._setup_signal_handlers()

    def _setup_signal_handlers(self):
        """Set up signal handlers for graceful shutdown."""
        if sys.platform != "win32":
            # Unix signals
            signal.signal(signal.SIGTERM, self._signal_handler)
            signal.signal(signal.SIGINT, self._signal_handler)
        else:
            # Windows signal handling
            signal.signal(signal.SIGINT, self._signal_handler)

    def _signal_handler(self, signum, _frame):
        """Handle shutdown signals."""
        logger.info("Received signal %s, initiating graceful shutdown...", signum)
        self.shutdown_event.set()

    async def wait_for_shutdown(self):
        """Wait for shutdown signal."""
        await self.shutdown_event.wait()

    async def cleanup(self):
        """Perform cleanup operations."""
        logger.info("Performing graceful shutdown cleanup...")
        # The inference service cleanup will be handled by FastAPI lifespan
        logger.info("Graceful shutdown completed")


# Global shutdown handler
shutdown_handler = GracefulShutdown()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan handler for non-blocking model initialization in background."""
    logger.info("Starting ColQwen2 inference server...")

    # Start model initialization asynchronously to avoid blocking FastAPI startup
    async def load_model_async():
        try:
            logger.info("Initializing ColQwen2 model (includes download if needed)...")
            await asyncio.to_thread(inference_service.initialize)
            logger.info("ColQwen2 model initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize model: {e}")
            # Don't raise - let health check endpoint handle the initialization state

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
    """Health check endpoint with proper HTTP status codes. Returns 503 during initialization, 200 when ready."""
    try:
        # Check ColQwen2 model initialization status
        model_loaded = inference_service.is_initialized

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
            "model_name": HEALTH_MODEL_NAME,
            "device": str(inference_service.device),
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
        # Return 503 Service Unavailable for initialization errors
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
    """Main embedding endpoint - generates ColQwen2 embeddings for images and text with multiple pooling strategies."""
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

        # Execute ColQwen2 inference in thread pool to prevent FastAPI event loop blocking
        result = await asyncio.to_thread(inference_service.generate_embeddings, request)

        return result

    except HTTPException:
        raise
    except ValueError as e:
        logger.error(f"Embedding generation failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Embedding generation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/", response_model=EmbedHealthResponse)
async def root():
    """Root endpoint - returns same as health check for convenience."""
    return await health_check()


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Global exception handler for unhandled errors in FastAPI endpoints."""
    logger.error(f"Unhandled exception: {exc}")
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "detail": str(exc)},
    )


def server_mode():
    """Start ColQwen2 inference server with Uvicorn and graceful shutdown handling."""
    logger.info("Starting ColQwen2 inference server mode...")

    import uvicorn

    # Configuration from environment variables
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8080"))
    log_level = os.getenv("LOG_LEVEL", "info").lower()

    logger.info(f"Starting ColQwen2 inference server on {host}:{port}")
    logger.info(f"Log level: {log_level}")
    logger.info(
        "Using single worker (required for shared model state and lifespan handlers)"
    )

    # Create uvicorn server configuration
    config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level=log_level,
        workers=1,  # Single worker required for shared ColQwen2 model state
        reload=False,
    )

    # Run server with graceful shutdown support
    server = uvicorn.Server(config)

    async def run_server():
        """Run the server with graceful shutdown monitoring."""
        # Start the server task
        server_task = asyncio.create_task(server.serve())

        # Wait for either server completion or shutdown signal
        shutdown_task = asyncio.create_task(shutdown_handler.wait_for_shutdown())

        try:
            done, pending = await asyncio.wait(
                {server_task, shutdown_task}, return_when=asyncio.FIRST_COMPLETED
            )

            # If shutdown was triggered, gracefully stop the server
            if shutdown_task in done:
                logger.info("Shutdown signal received, stopping server...")
                server.should_exit = True
                await server_task
                await shutdown_handler.cleanup()

            # Cancel any pending tasks
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        except Exception as e:
            logger.error(f"Server error: {e}")
            raise

    # Run the server with asyncio
    asyncio.run(run_server())


@trace_operation("model_download")
def download_mode():
    """Init container mode - downloads ColQwen2 model to persistent volume then exits."""
    logger.info("ColQwen2 container starting in download mode")

    # Get paths from environment variables
    model_directory = os.getenv("MODEL_DIRECTORY_PATH", "/tmp/model-directory")

    logger.info("Init container: Starting model download...")
    logger.info("Model directory: %s", model_directory)

    try:
        # Download ColQwen2 model using inference service download functionality
        inference_service._download_model_if_needed(model_directory)
        logger.info("Init container: Model download completed successfully")
    except Exception as e:
        logger.error(f"Model download failed: {e}")
        raise


def main():
    """Main entry point - supports both init container (download) and inference server modes."""
    # Check for download mode
    if len(sys.argv) > 1 and sys.argv[1] == "download":
        download_mode()
    else:
        logger.info("ColQwen2 container starting as inference server")
        server_mode()


if __name__ == "__main__":
    main()
