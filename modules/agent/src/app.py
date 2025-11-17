# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""
Azure OpenAI Agent API Server

FastAPI server for document Q&A using Azure AI Agents and ColQwen2.
Provides REST API endpoints for conversational document retrieval with source citations.
"""

import asyncio
import json
import logging
import os
import signal
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from dotenv import find_dotenv, load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from .agent import ColQwenAgent
from .document_retriever import DocumentRetriever
from .models import (
    ChatRequest,
    HealthResponse,
)
from .setup_logging import configure_telemetry

# Find and load .env file from the project root
load_dotenv(find_dotenv())

# Configure telemetry on module import
configure_telemetry()

logger = logging.getLogger(__name__)

# Global agent instance
agent: Optional[ColQwenAgent] = None
retriever_service: Optional[DocumentRetriever] = None


class GracefulShutdown:
    """Handle graceful shutdown for Azure OpenAI Agent API server."""

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
        # The agent cleanup will be handled by FastAPI lifespan
        logger.info("Graceful shutdown completed")


# Global shutdown handler (don't set up signals yet)
shutdown_handler = GracefulShutdown()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan handler for agent initialization."""
    global agent
    logger.info("Starting Azure OpenAI Agent API Server...")

    try:
        logger.info("Initializing ColQwen Agent...")
        agent = ColQwenAgent()
        await agent.initialize()
        logger.info("Agent initialized successfully and ready")
        yield
    finally:
        logger.info("Shutting down Azure OpenAI Agent API Server...")
        if agent is not None:
            await agent.cleanup()


# Create FastAPI app
app = FastAPI(
    title="Azure OpenAI Agent API",
    description="Document Q&A assistant using Azure AI Agents and ColQwen2 for visual document retrieval",
    version="1.0.0",
    lifespan=lifespan,
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_agent() -> ColQwenAgent:
    """Get the agent instance."""
    if agent is None:
        raise RuntimeError("Agent not initialized")
    return agent


# API Endpoints
@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint."""
    return HealthResponse(status="healthy", timestamp=datetime.utcnow().isoformat())


@app.post("/chat")
async def chat(request: ChatRequest):
    """
    Chat with the agent (non-streaming).

    Args:
        request: Chat request with message and optional chat history

    Returns:
        JSON response with the complete agent response
    """
    try:
        logger.info(f"Received chat request: {request.message[:50]}...")

        agent_instance = get_agent()

        # Collect all streaming events into a single response
        response_content = ""
        sources = []

        async for event in agent_instance.run_stream(
            message=request.message,
            history=request.history,
        ):
            if event.get("type") == "text_delta":
                response_content += event.get("content", "")
            elif event.get("type") == "done":
                sources = event.get("sources", [])
                break
            elif event.get("type") == "error":
                raise RuntimeError(event.get("content", "Unknown error"))

        return {
            "response": response_content,
            "sources": sources,
        }

    except Exception as e:
        logger.error(f"Error in chat: {str(e)}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": "Internal server error", "detail": str(e)},
        )


@app.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    """
    Chat with the agent using Server-Sent Events (streaming).

    Streams real-time events including:
    - text_delta: Incremental text responses from the agent
    - tool_start: When the agent starts using a tool (e.g., search_documents)
    - done: When the response is complete
    - error: If an error occurs during processing

    Each event includes search_count and total_searches for monitoring.

    Args:
        request: Chat request with message and optional chat history

    Returns:
        StreamingResponse with Server-Sent Events
    """

    async def event_generator():
        try:
            logger.info(f"Received streaming chat request: {request.message[:50]}...")

            agent_instance = get_agent()

            async for event in agent_instance.run_stream(
                message=request.message,
                history=request.history,
            ):
                # Yield SSE event in correct format
                event_json = json.dumps(event)
                sse_line = f"data: {event_json}\n\n"
                logger.debug(
                    f"Sending SSE event: {event.get('type', 'unknown')} - Length: {len(sse_line)}"
                )
                yield sse_line

            logger.info("Streaming completed successfully")

        except Exception as e:
            logger.error(f"Error in streaming: {str(e)}", exc_info=True)
            # Send error event
            error_event = {
                "type": "error",
                "content": str(e),
                "timestamp": datetime.utcnow().isoformat(),
            }
            error_json = json.dumps(error_event)
            error_sse = f"data: {error_json}\n\n"
            logger.debug(f"Sending error SSE event: {error_sse}")
            yield error_sse

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Global exception handler for unhandled errors in FastAPI endpoints."""
    logger.error(f"Unhandled exception: {exc}")
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "detail": str(exc)},
    )


def server_mode():
    """Start Azure OpenAI Agent API server with Uvicorn and graceful shutdown handling."""
    logger.info("Starting Azure OpenAI Agent API server mode...")

    import uvicorn

    # Configuration from environment variables (Docker default PORT=8080)
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8080"))
    log_level = os.getenv("LOG_LEVEL", "info").lower()

    logger.info(f"Starting Azure OpenAI Agent API server on {host}:{port}")
    logger.info(f"Log level: {log_level}")
    logger.info(
        "Using single worker (required for shared agent state and lifespan handlers)"
    )

    # Create uvicorn server configuration
    config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level=log_level,
        workers=1,  # Single worker required for shared agent state
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


def main():
    """Main entry point for Azure OpenAI Agent API server."""
    logger.info("Azure OpenAI Agent container starting as API server")
    server_mode()


if __name__ == "__main__":
    main()
