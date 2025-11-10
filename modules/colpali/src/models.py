"""
Pydantic models for the ColQwen2 embedding API.

This module contains all the request and response models used by the FastAPI endpoints.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class EmbedHealthResponse(BaseModel):
    """Health check response."""

    status: str
    model_loaded: bool
    model_info: Optional[Dict[str, Any]] = None
    message: Optional[str] = None
    error: Optional[str] = None


class EmbedRequest(BaseModel):
    """Request model for embedding API."""

    texts: Optional[List[str]] = Field(
        default=None, description="List of texts to embed"
    )
    images: Optional[List[str]] = Field(
        default=None,
        description="List of base64-encoded images or data URIs (e.g., 'data:image/png;base64,iVBORw...')",
    )

    pooling_type: List[str] = Field(
        default=["hierarchical"],
        description="List of pooling types: 'none', 'hierarchical', 'mean_pooling'",
    )
    pooling_config: Optional[Dict[str, Any]] = Field(
        default=None, description="Optional pooling configuration"
    )


class EmbedResponse(BaseModel):
    """Response model for embedding API."""

    embeddings: Optional[List[List[List[float]]]] = Field(
        default=None,
        description="Generated embeddings (batch x patches x embedding_dim)",
    )

    # Hierarchical structure for different pooling types
    hierarchical_pooled_embeddings: Optional[Dict[str, List[List[List[float]]]]] = (
        Field(default=None, description="Hierarchical embeddings by pooling type")
    )

    mean_row_pooled_embeddings: Optional[Dict[str, List[List[List[float]]]]] = Field(
        default=None, description="Mean row pooled embeddings"
    )

    mean_column_pooled_embeddings: Optional[Dict[str, List[List[List[float]]]]] = Field(
        default=None, description="Mean column pooled embeddings"
    )
