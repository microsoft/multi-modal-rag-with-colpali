# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Chainlit UI for the ColQwen document Q&A agent."""

import asyncio
import base64
import logging
import os
from typing import List

import chainlit as cl
import httpx
from dotenv import find_dotenv, load_dotenv

# Find and load .env file from the project root
load_dotenv(find_dotenv())

logger = logging.getLogger(__name__)

# Configuration
# Default to Kubernetes service DNS when running in cluster
AGENT_API_URL = os.getenv("AGENT_API_URL", "http://agent-api-service:8000")
REQUEST_TIMEOUT = 120.0  # 2 minutes for document search


async def check_agent_health() -> bool:
    """Check if the agent API is healthy.

    Returns:
        bool: True if the agent API responds with HTTP 200, otherwise False.
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{AGENT_API_URL}/health", timeout=5.0)
            return response.status_code == 200
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return False


def _get_chat_history() -> List[dict]:
    """Return accumulated chat history from the user session.

    History is a list of {"user", "assistant"} turns.
    """
    history = cl.user_session.get("history", [])
    logger.debug("[UI] Retrieved chat history with %d turns", len(history))
    return history


def _append_to_history(user_message: str, assistant_message: str) -> None:
    """Append a new turn to the in-memory chat history."""
    history = cl.user_session.get("history", [])
    history.append({"user": user_message, "assistant": assistant_message})
    cl.user_session.set("history", history)
    logger.debug("[UI] Appended new turn. History length is now %d", len(history))


async def send_message(message: str) -> dict:
    """Send a message to the agent API (non-streaming).

    Args:
        message: User message to send.

    Returns:
        dict: JSON response from the agent API.
    """
    history = _get_chat_history()
    logger.debug(
        "[UI] Sending non-stream request. Message length=%d, history_turns=%d",
        len(message),
        len(history),
    )
    payload = {"message": message, "history": history}

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{AGENT_API_URL}/chat", json=payload, timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        return response.json()


async def send_message_stream(message: str):
    """Send a message to the agent API with streaming response.

    Args:
        message: User message to send.

    Yields:
        dict: Streaming event payloads from the agent API.
    """
    import json

    history = _get_chat_history()
    logger.debug(
        "[UI] Sending stream request. Message length=%d, history_turns=%d",
        len(message),
        len(history),
    )
    payload = {"message": message, "history": history}

    # Configure timeout for streaming: quick connect, long read
    # connect: 10s to establish connection
    # read: None (no timeout on reading chunks - streaming can take time)
    # write: 30s to send request
    # pool: 10s to acquire connection from pool
    timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        headers = {
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
        }
        async with client.stream(
            "POST", f"{AGENT_API_URL}/chat/stream", json=payload, headers=headers
        ) as response:
            response.raise_for_status()
            logger.info(
                f"Streaming connection established. Content-Type: {response.headers.get('content-type')}"
            )

            async for line in response.aiter_lines():
                # Handle both string and bytes responses
                if isinstance(line, bytes):
                    line = line.decode("utf-8")

                # Skip empty lines
                if not line.strip():
                    continue

                if line.startswith("data: "):
                    data_content = line[
                        6:
                    ].strip()  # Remove "data: " prefix and strip whitespace

                    # Skip empty data lines
                    if not data_content:
                        continue

                    try:
                        event_data = json.loads(data_content)
                        yield event_data
                    except json.JSONDecodeError as e:
                        logger.warning(
                            f"Failed to parse SSE data: '{data_content}', error: {e}"
                        )
                        # Log the raw data for debugging
                        logger.debug(
                            f"Raw line: {repr(line)}, extracted data: {repr(data_content)}"
                        )


def create_image_element_from_base64(
    base64_data: str, name: str, display: str = "side"
) -> cl.Image:
    """Create a Chainlit Image element from base64 data.

    Args:
        base64_data: Base64-encoded image contents.
        name: Name to display for the image.
        display: Chainlit display mode (for example, "side").

    Returns:
        cl.Image: Image element ready to attach to a message.

    Raises:
        ValueError: If the base64 data cannot be decoded.
    """
    try:
        image_bytes = base64.b64decode(base64_data)
        return cl.Image(content=image_bytes, name=name, display=display)
    except Exception as e:
        logger.error(f"Failed to decode base64 image data for {name}: {e}")
        raise ValueError(f"Invalid base64 image data: {e}")


@cl.set_starters
async def set_starters() -> List[cl.Starter]:
    """Return default conversation starters for the agent.

    Returns:
        list[cl.Starter]: Suggested starter prompts.
    """
    return [
        cl.Starter(
            label="How does the document search system work?",
            message="How does the document search system work?",
        ),
        cl.Starter(
            label="How does ColPali scale for large document collections?",
            message="How does ColPali scale for large document collections?",
        ),
        cl.Starter(
            label="Can you explain how document patching works in ColPali?",
            message="Can you explain how document patching works in ColPali?",
        ),
    ]


@cl.on_chat_start
async def on_chat_start():
    """Initialize the chat session.

    Performs a health check against the agent API and sets
    per-session state for the Chainlit user session.
    """
    try:
        # Check agent health
        is_healthy = await check_agent_health()

        if not is_healthy:
            await cl.Message(
                content="**Agent API is not available**\n\n"
                f"Please ensure the agent API is running at: `{AGENT_API_URL}`"
            ).send()
            cl.user_session.set("agent_available", False)
            return

        # Initialize session
        cl.user_session.set("history", [])
        logger.debug("[UI] Initialized empty chat history for new session")
        cl.user_session.set("agent_available", True)

        # Don't send welcome message - let user initiate conversation naturally
        logger.info(
            "Chat session initialized successfully - agent is healthy and ready"
        )

    except Exception as e:
        logger.error(f"Failed to initialize chat: {str(e)}", exc_info=True)
        await cl.Message(
            content=f"Failed to initialize: {str(e)}\n\nPlease refresh the page and try again."
        ).send()
        cl.user_session.set("agent_available", False)


@cl.on_message
async def on_message(message: cl.Message):
    """Handle incoming messages with streaming.

    Args:
        message: Incoming Chainlit message from the user.
    """
    # Check if agent is available
    if not cl.user_session.get("agent_available", False):
        await cl.Message(
            content="Agent is not available. Please refresh the page."
        ).send()
        return

    try:
        logger.info(f"Processing message: {message.content[:50]}...")

        # Create message for streaming response
        msg = cl.Message(content="")
        await msg.send()

        # Track state
        current_step = None
        search_count = 0
        image_elements = []
        events_received = 0

        # Token buffering for efficient streaming
        token_buffer = ""
        buffer_size = (
            8  # Buffer up to 8 characters before streaming (better for readability)
        )
        last_stream_time = 0
        min_stream_interval = (
            0.03  # Minimum 30ms between UI updates (smooth but not overwhelming)
        )
        max_buffer_time = (
            0.15  # Maximum 150ms to hold tokens (prevents lag on slow responses)
        )

        # Stream events from agent
        async for event in send_message_stream(message.content):
            events_received += 1
            event_type = event.get("type")
            logger.debug(f"Received event #{events_received}: type={event_type}")

            if event_type == "text_delta":
                # Buffer tokens for efficient streaming
                content = event.get("content", "")
                if content:
                    token_buffer += content
                    current_time = asyncio.get_event_loop().time()
                    time_since_last_stream = current_time - last_stream_time

                    # Stream when buffer is full, enough time has passed, or buffer is getting old
                    should_stream = (
                        len(token_buffer) >= buffer_size
                        or time_since_last_stream >= min_stream_interval
                        or (token_buffer and time_since_last_stream >= max_buffer_time)
                    )

                    if should_stream and token_buffer:
                        await msg.stream_token(token_buffer)
                        token_buffer = ""
                        last_stream_time = current_time

            elif event_type == "step_start":
                # Start a new step
                step_name = event.get("name", "Processing")
                step_type = event.get("type_name", "tool")
                step_input = event.get("input", "")

                current_step = cl.Step(name=step_name, type=step_type)
                if step_input:
                    current_step.input = step_input
                await current_step.send()
                logger.debug(f"Started step: {step_name}")

            elif event_type == "step_end":
                # End the current step
                step_output = event.get("output", "Completed")
                if current_step:
                    current_step.output = step_output
                    await current_step.update()
                    logger.debug(f"Completed step: {current_step.name}")
                    current_step = None  # Reset for next step

            elif event_type == "tool_start":
                # Legacy tool start event (fallback)
                tool_name = event.get("tool_name", "unknown")
                search_count = event.get("search_count", 0)

                if tool_name == "search_documents":
                    if current_step is None:
                        current_step = cl.Step(
                            name=f"Search #{search_count}", type="tool"
                        )
                        await current_step.send()
                    else:
                        # Update existing step with new count
                        current_step.name = f"Search #{search_count}"
                        await current_step.update()

            elif event_type == "source_delta":
                # Handle incremental source information (no text injection)
                source = event.get("source", {})
                base64_data = source.get("base64_image")

                if base64_data:
                    try:
                        # Use the citation-style label (e.g. "[1]") as the image name
                        source_id = source.get("source", "")
                        image_name = (
                            f"[{source_id.split('_')[-1]}]"
                            if source_id.startswith("source_")
                            else (source_id or "[source]")
                        )

                        image_element = create_image_element_from_base64(
                            base64_data,
                            name=image_name,
                            display="side",
                        )
                        image_elements.append(image_element)
                        logger.debug(
                            f"Created image element for incremental source: {source.get('title', 'Unknown')}"
                        )
                    except (ValueError, Exception) as e:
                        logger.error(
                            f"Failed to create image element for incremental source ({source.get('title', 'Unknown')}): {e}"
                        )

            elif event_type == "done":
                # Complete the response
                # Close any remaining step
                if current_step:
                    current_step.output = "Completed"
                    await current_step.update()

                # Ensure message has any accumulated image elements (no source text)
                if image_elements:
                    msg.elements = image_elements
                    await msg.update()
                    logger.info(
                        f"Updated message with {len(image_elements)} image elements"
                    )

                # Stream any remaining buffered tokens
                if token_buffer.strip():
                    await msg.stream_token(token_buffer)
                    token_buffer = ""

                # Append completed turn to history
                logger.debug(
                    "[UI] Completed assistant message with length=%d, updating history",
                    len(msg.content or ""),
                )
                _append_to_history(message.content, msg.content)

                logger.info(f"Processed {events_received} events successfully")
                break

            elif event_type == "error":
                # Stream any remaining buffered tokens before error
                if token_buffer.strip():
                    await msg.stream_token(token_buffer)
                    token_buffer = ""

                # Handle error
                error_msg = event.get("content", "Unknown error")
                logger.error(f"Agent error: {error_msg}")
                await msg.stream_token(f"\n\n**Error:** {error_msg}")
                break

            else:
                # Log unhandled event types for debugging
                logger.debug(f"Unhandled event type: {event_type}")
                # Don't log full event content as it can be very verbose

    except httpx.TimeoutException:
        logger.error("Request timed out")
        await cl.Message(
            content="Request timed out. The search is taking longer than expected. Please try again."
        ).send()
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error: {e}")
        await cl.Message(
            content=f"Agent API error: {e.response.status_code}\n\n{e.response.text}"
        ).send()
    except Exception as e:
        logger.error(f"Error processing message: {str(e)}", exc_info=True)
        await cl.Message(content=f"Sorry, I encountered an error: {str(e)}").send()


@cl.on_chat_end
async def on_chat_end():
    """Clean up when chat ends."""
    logger.info("Chat session ended")
