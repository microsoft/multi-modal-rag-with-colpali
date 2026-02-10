# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""
ColPali Inference Service

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

from dotenv import find_dotenv, load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from .inference import ColPaliInference
from .models import EmbedHealthResponse, EmbedRequest, EmbedResponse
from .setup_logging import configure_telemetry, trace_operation

# Find and load .env file from the project root
load_dotenv(find_dotenv())

# Configure telemetry on module import
configure_telemetry()

logger = logging.getLogger(__name__)

# Global inference instance
inference_service = ColPaliInference()
HEALTH_MODEL_NAME = os.getenv("MODEL_ID", "vidore/colqwen2-v1.0-hf")


class GracefulShutdown:
    """Handle graceful shutdown for ColPali inference service."""

    def __init__(self):
        self.shutdown_event = asyncio.Event()
        self.shutdown_initiated = False

    def setup_signal_handlers(self):
        """Set up signal handlers for graceful shutdown (call from async context)."""

        def signal_handler(signum):
            if not self.shutdown_initiated:
                self.shutdown_initiated = True
                logger.info(
                    "Received signal %s, initiating graceful shutdown...", signum
                )
                # Schedule the shutdown event to be set in the event loop
                asyncio.create_task(self._set_shutdown_event())
            else:
                logger.info("Shutdown already initiated, ignoring signal %s", signum)

        # Set up signal handlers in the event loop
        loop = asyncio.get_running_loop()

        if sys.platform != "win32":
            # Unix signals
            loop.add_signal_handler(signal.SIGTERM, signal_handler, signal.SIGTERM)
            loop.add_signal_handler(signal.SIGINT, signal_handler, signal.SIGINT)
        else:
            # Windows signal handling (fallback to traditional signal handling)
            def sync_signal_handler(signum, _frame):
                signal_handler(signum)

            signal.signal(signal.SIGINT, sync_signal_handler)

    async def _set_shutdown_event(self):
        """Set the shutdown event."""
        self.shutdown_event.set()

    async def wait_for_shutdown(self):
        """Wait for shutdown signal."""
        await self.shutdown_event.wait()

    async def cleanup(self):
        """Perform cleanup operations."""
        logger.info("Performing graceful shutdown cleanup...")
        # The inference service cleanup will be handled by FastAPI lifespan
        logger.info("Graceful shutdown completed")


# Global shutdown handler (don't set up signals yet)
shutdown_handler = GracefulShutdown()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan handler for non-blocking model initialization in background."""
    logger.info("Starting ColPali inference server...")

    # Start model initialization asynchronously to avoid blocking FastAPI startup
    async def load_model_async():
        try:
            logger.info("Initializing ColPali model (includes download if needed)...")
            await asyncio.to_thread(inference_service.initialize)
            logger.info("ColPali model initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize model: {e}")
            # Don't raise - let health check endpoint handle the initialization state

    # Start model loading in background
    asyncio.create_task(load_model_async())

    try:
        yield
    finally:
        logger.info("Shutting down ColPali inference server...")


# Create FastAPI app directly
app = FastAPI(
    title="ColPali Inference Service",
    description="FastAPI service for ColPali document understanding and embedding generation",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", response_model=EmbedHealthResponse)
async def health_check():
    """Health check endpoint with proper HTTP status codes. Returns 503 during initialization, 200 when ready."""
    try:
        # Check ColPali model initialization status
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
    """Main embedding endpoint - generates ColPali embeddings for images and text with multiple pooling strategies."""
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

        # Execute ColPali inference in thread pool to prevent FastAPI event loop blocking
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
    """Start ColPali inference server with Uvicorn and graceful shutdown handling."""
    logger.info("Starting ColPali inference server mode...")

    import uvicorn

    # Configuration from environment variables
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8080"))
    log_level = os.getenv("LOG_LEVEL", "info").lower()

    logger.info(f"Starting ColPali inference server on {host}:{port}")
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
        workers=1,  # Single worker required for shared ColPalimodel state
        reload=False,
    )

    # Run server with graceful shutdown support
    server = uvicorn.Server(config)

    async def run_server():
        """Run the server with graceful shutdown monitoring."""
        # Set up signal handlers in the event loop
        shutdown_handler.setup_signal_handlers()

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
    """Init container mode - downloads ColPali model to persistent volume then exits."""
    logger.info("ColPali container starting in download mode")
    logger.info("Init container: Starting model download...")

    try:
        # Download ColPali model using inference service download functionality
        inference_service._download_model_if_needed()
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
        logger.info("ColPali container starting as inference server")
        server_mode()


if __name__ == "__main__":
    main()
