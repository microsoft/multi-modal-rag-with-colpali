# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
# ColPali related code taken from: https://github.com/microsoft/dstoolkit-multi-modal-rag-with-colpali
"""
ColPali Inference Service — CPU-only FastAPI shim.

Provides REST API endpoints for health checks and embedding generation
with multiple pooling strategies. The GPU forward pass is delegated to a
vLLM sidecar container in the same pod; this process owns only the HF
processor (for tokenization + patch-grid math) and the pooling
post-processing.

Runs with multiple uvicorn worker processes — each worker has its own
processor instance and its own aiohttp.ClientSession to vLLM. vLLM owns
GPU concurrency via continuous batching across all workers.

Init container mode (`python -m colpali_inference.app download`) handles
model snapshot population from HuggingFace Hub into the per-replica PVC;
see download_model.py.
"""

import asyncio
import logging
import os
import signal
import sys
from contextlib import asynccontextmanager

from dotenv import find_dotenv, load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from .inference import get_inference_instance
from .models import EmbedHealthResponse, EmbedRequest, EmbedResponse
from .setup_logging import configure_telemetry, trace_operation
from .vllm_client import VLLMUpstreamError, get_client

# Find and load .env file from the project root
load_dotenv(find_dotenv())

# Configure telemetry on module import
configure_telemetry()

logger = logging.getLogger(__name__)

# Per-worker singletons (one of each per uvicorn worker process)
inference_service = get_inference_instance()
vllm_client = get_client()

HEALTH_MODEL_NAME = os.getenv("MODEL_ID", "TomoroAI/tomoro-colqwen3-embed-4b")
VLLM_READY_TIMEOUT_SECONDS = float(os.getenv("VLLM_READY_TIMEOUT_SECONDS", "300"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan handler.

    Loads the HF processor synchronously (fast — CPU only, ~100 MB) and kicks
    off a background task that waits for the vLLM sidecar to become ready.
    /health reports the combined status.
    """
    logger.info("Starting ColPali inference shim...")

    # Sweep any stale image files left in IMAGE_SHM_DIR by a previous
    # container instance that was killed mid-request (kill -9, OOM, segfault)
    # before its `_stage_images` finally-block could unlink them. emptyDir
    # tmpfs survives in-place container restarts, so without this sweep the
    # 2Gi /shm would slowly fill across restarts.
    try:
        shm_dir = vllm_client.image_shm_dir
        shm_dir.mkdir(parents=True, exist_ok=True)
        stale = list(shm_dir.glob("*.png"))
        for p in stale:
            try:
                p.unlink(missing_ok=True)
            except OSError as e:
                logger.warning("Failed to remove stale shm file %s: %s", p, e)
        if stale:
            logger.info("Cleared %d stale image(s) from %s", len(stale), shm_dir)
    except Exception as e:
        logger.warning("Stale shm sweep failed: %s", e)

    # Processor load is fast on CPU; do it inline so the worker is ready
    # to serve as soon as vLLM is also ready.
    try:
        inference_service.initialize()
    except Exception as e:
        logger.error("Failed to initialize processor: %s", e)
        # Don't raise — health check will report unhealthy.

    async def wait_for_vllm():
        try:
            await vllm_client.wait_until_ready(
                timeout_seconds=VLLM_READY_TIMEOUT_SECONDS
            )
        except Exception as e:
            logger.error("vLLM sidecar did not become ready: %s", e)

    asyncio.create_task(wait_for_vllm())

    try:
        yield
    finally:
        logger.info("Shutting down ColPali inference shim...")
        await vllm_client.close()


app = FastAPI(
    title="ColPali Inference Service",
    description="FastAPI shim for ColPali / ColQwen3 embedding generation (vLLM-backed)",
    version="2.0.0",
    lifespan=lifespan,
)

FastAPIInstrumentor.instrument_app(app)


@app.get("/health", response_model=EmbedHealthResponse)
async def health_check():
    """Combined health check: processor loaded AND vLLM sidecar reachable.

    Returns 200 when both are ready, 503 otherwise. Used by both readiness
    and liveness probes (separate liveness `/livez` skips the vLLM check).
    """
    try:
        processor_loaded = inference_service.is_initialized
        vllm_ready = await vllm_client.health()

        if not processor_loaded:
            raise HTTPException(
                status_code=503,
                detail=EmbedHealthResponse(
                    status="initializing",
                    model_loaded=False,
                    model_info=None,
                    message="Processor still loading",
                ).model_dump(),
            )

        if not vllm_ready:
            raise HTTPException(
                status_code=503,
                detail=EmbedHealthResponse(
                    status="initializing",
                    model_loaded=False,
                    model_info={"vllm_ready": False},
                    message="vLLM sidecar not ready",
                ).model_dump(),
            )

        return EmbedHealthResponse(
            status="healthy",
            model_loaded=True,
            model_info={
                "model_name": HEALTH_MODEL_NAME,
                "vllm_base_url": vllm_client.base_url,
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Health check failed: %s", e)
        raise HTTPException(
            status_code=503,
            detail=EmbedHealthResponse(
                status="unhealthy",
                model_loaded=False,
                model_info=None,
                error=str(e),
            ).model_dump(),
        )


@app.get("/livez")
async def liveness_check():
    """Lightweight liveness probe — does not touch vLLM.

    Returns 200 as long as the shim process is responding. vLLM has its
    own liveness probe configured on the sidecar container.
    """
    return {"status": "alive"}


@app.post("/embeddings", response_model=EmbedResponse)
async def generate_embeddings_endpoint(request: EmbedRequest, raw_request: Request):
    """Main embedding endpoint.

    Supports client-disconnect detection: if the caller drops the connection
    while inference is in progress, the inference task is cancelled and the
    in-flight HTTP call to vLLM is aborted via aiohttp's task cancellation.
    """
    try:
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

        inference_task = asyncio.create_task(
            inference_service.generate_embeddings(request)
        )

        async def watch_disconnect() -> bool:
            while not inference_task.done():
                if await raw_request.is_disconnected():
                    return True
                await asyncio.sleep(0.5)
            return False

        disconnect_task = asyncio.create_task(watch_disconnect())

        try:
            done, _ = await asyncio.wait(
                {inference_task, disconnect_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if disconnect_task in done and disconnect_task.result():
                logger.warning(
                    "Client disconnected during inference, cancelling vLLM call"
                )
                inference_task.cancel()
                return JSONResponse(
                    status_code=499, content={"detail": "Client disconnected"}
                )

            return inference_task.result()

        finally:
            for task in (inference_task, disconnect_task):
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass

    except HTTPException:
        raise
    except ValueError as e:
        logger.error("Embedding generation failed: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
    except VLLMUpstreamError as e:
        # Surface vLLM rejections verbatim so callers (indexer, search) can
        # back off appropriately. The shim does not retry.
        if e.status_code == 429:
            headers = {"Retry-After": e.retry_after} if e.retry_after else None
            logger.warning(
                "vLLM 429 propagated to caller (retry-after=%s)", e.retry_after
            )
            raise HTTPException(
                status_code=429,
                detail="vLLM is overloaded; retry with backoff",
                headers=headers,
            )
        if e.status_code == 503:
            headers = {"Retry-After": e.retry_after} if e.retry_after else None
            logger.warning(
                "vLLM 503 propagated to caller (retry-after=%s)", e.retry_after
            )
            raise HTTPException(
                status_code=503,
                detail="vLLM is unavailable",
                headers=headers,
            )
        # Other upstream failures: signal a bad gateway, not an internal
        # shim error — distinguishes "vLLM broke" from "shim broke".
        logger.error(
            "vLLM upstream error %s propagated as 502: %s",
            e.status_code,
            e.body[:200],
        )
        raise HTTPException(
            status_code=502,
            detail=f"vLLM upstream error (status {e.status_code})",
        )
    except Exception as e:
        logger.error("Embedding generation failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/", response_model=EmbedHealthResponse)
async def root():
    """Root endpoint - returns same as health check for convenience."""
    return await health_check()


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Global exception handler for unhandled errors."""
    logger.error("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "detail": str(exc)},
    )


def server_mode() -> None:
    """Start the uvicorn server with multiple workers.

    The shim is CPU-bound (tokenize, decode, pool); multiple workers
    parallelize across cores. All workers funnel into the single vLLM
    sidecar which owns GPU continuous batching.
    """
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    log_level = os.getenv("LOG_LEVEL", "info").lower()
    workers = int(os.getenv("UVICORN_WORKERS", "4"))
    limit_concurrency_str = os.getenv("LIMIT_CONCURRENCY")
    limit_concurrency = int(limit_concurrency_str) if limit_concurrency_str else None

    logger.info("Starting ColPali inference shim on %s:%s", host, port)
    logger.info("Log level: %s", log_level)
    logger.info("Uvicorn workers: %s", workers)
    if limit_concurrency:
        logger.info("Per-worker concurrency limit: %s", limit_concurrency)

    # Use uvicorn.run with the import string when running multiple workers
    # (uvicorn requires the import string to reload the app in each worker
    # process).
    uvicorn.run(
        "colpali_inference.app:app",
        host=host,
        port=port,
        log_level=log_level,
        workers=workers,
        reload=False,
        limit_concurrency=limit_concurrency,
    )


@trace_operation("model_download")
def download_mode() -> None:
    """Init container mode — downloads model snapshot to the shared PVC, then exits.

    Pulls the model from HuggingFace Hub into
    ``${MODEL_DIRECTORY_PATH}/<basename(MODEL_ID)>`` so both the vLLM
    sidecar and the shim processor load from the same on-disk snapshot.
    """
    from .download_model import download_model_from_hf

    logger.info("ColPali container starting in download mode")
    target_path = download_model_from_hf()
    logger.info("Init container: model download completed to %s", target_path)


def _install_signal_handlers() -> None:
    """SIGTERM / SIGINT handlers for graceful shutdown.

    With multiple uvicorn workers, the master process handles signals and
    propagates to workers automatically. We only need to ensure SIGTERM
    triggers a clean exit (uvicorn's default behaviour already does this).
    """
    if sys.platform == "win32":
        return

    def _handler(signum, _frame):
        logger.info("Received signal %s, shutting down", signum)
        # Uvicorn master will catch and propagate; just log here.

    signal.signal(signal.SIGTERM, _handler)


def main() -> None:
    """Main entry point — supports init container (download) and server modes."""
    if len(sys.argv) > 1 and sys.argv[1] == "download":
        download_mode()
    else:
        _install_signal_handlers()
        logger.info("ColPali container starting as inference shim")
        server_mode()


if __name__ == "__main__":
    main()
