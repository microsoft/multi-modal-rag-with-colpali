# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""
Pydantic models for Azure OpenAI Agent API.

This module contains all the request and response models used by the FastAPI endpoints.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ChatTurn(BaseModel):
    """Single turn in the chat history."""

    user: str = Field(..., description="User message")
    assistant: Optional[str] = Field(
        default=None, description="Assistant response for this turn, if available"
    )


class ChatRequest(BaseModel):
    """Request model for chat endpoint."""

    message: str = Field(..., description="User's question or message")
    history: List[ChatTurn] = Field(
        default_factory=list,
        description=(
            "Ordered list of previous user/assistant turns to provide conversational context. "
            "The latest user question should be in `message`, not duplicated here."
        ),
    )


class ChatResponse(BaseModel):
    """Response model for chat endpoint."""

    response: str = Field(..., description="Agent's response to the user's message")
    sources: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="List of source documents referenced in the response",
    )
    search_count: int = Field(
        default=0, description="Number of document searches performed"
    )
    tool_calls: List[str] = Field(
        default_factory=list,
        description="List of tool calls made during the conversation",
    )


class HealthResponse(BaseModel):
    """Response model for health check."""

    status: str = Field(..., description="Service health status")
    timestamp: str = Field(..., description="ISO 8601 timestamp of the health check")
