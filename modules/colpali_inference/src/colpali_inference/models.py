# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""
Pydantic models for the ColPali embedding API.

This module contains all the request and response models used by the FastAPI endpoints.
"""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, ValidationInfo, field_validator


class PoolingType(str, Enum):
    """Enumeration for different pooling types."""

    NONE = "none"
    HIERARCHICAL_POOLING = "hierarchical_pooling"
    MEAN_POOLING = "mean_pooling"


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

    pooling_type: List[PoolingType] = Field(
        default=[PoolingType.NONE],
        description="List of pooling types. Note: Only 'none' is supported for text queries.",
        validate_default=True,
    )
    pooling_config: Optional[Dict[str, Any]] = Field(
        default=None, description="Optional pooling configuration"
    )

    @field_validator("pooling_type")
    @classmethod
    def validate_pooling_for_text(
        cls, v: List[PoolingType], info: ValidationInfo
    ) -> List[PoolingType]:
        """Validate that pooling is not requested for text queries."""
        # Get the texts field from the model being validated
        texts = info.data.get("texts")
        images = info.data.get("images")

        # If this is a text-only request, check for invalid pooling types
        if texts and not images:
            invalid_pooling_types = [p for p in v if p != PoolingType.NONE]
            if invalid_pooling_types:
                invalid_names = [p.value for p in invalid_pooling_types]
                raise ValueError(
                    f"Pooling types {invalid_names} are not supported for text queries. "
                    "Text queries return single embeddings, so only 'none' pooling is applicable. "
                    f"Remove pooling_type parameter or set it to [{PoolingType.NONE.value!r}] for text requests."
                )

        return v

    @field_validator("texts")
    @classmethod
    def validate_texts_content(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        """Validate that all text items are not None and not empty."""
        if v is not None:
            for i, text in enumerate(v):
                if text is None:
                    raise ValueError(f"Text at index {i} cannot be None")
                if not isinstance(text, str):
                    raise ValueError(
                        f"Text at index {i} must be a string, got {type(text)}"
                    )
                if len(text.strip()) == 0:
                    raise ValueError(
                        f"Text at index {i} cannot be empty or contain only whitespace"
                    )
        return v

    @field_validator("images")
    @classmethod
    def validate_images_content(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        """Validate that all image items are not None and not empty."""
        if v is not None:
            for i, image in enumerate(v):
                if image is None:
                    raise ValueError(f"Image at index {i} cannot be None")
                if not isinstance(image, str):
                    raise ValueError(
                        f"Image at index {i} must be a string, got {type(image)}"
                    )
                if len(image.strip()) == 0:
                    raise ValueError(
                        f"Image at index {i} cannot be empty or contain only whitespace"
                    )
        return v


class EmbedResponse(BaseModel):
    """Response model for embedding API."""

    embeddings: Optional[List[List[List[float]]]] = Field(
        default=None,
        description="Generated embeddings (batch x patches x embedding_dim)",
    )

    hierarchical_pooled_embeddings: Optional[List[List[List[float]]]] = Field(
        default=None,
        description="Hierarchical embeddings by pooling type (batch x hierarchical patches x embedding_dim)",
    )

    mean_row_pooled_embeddings: Optional[List[List[List[float]]]] = Field(
        default=None,
        description="Mean row pooled embeddings (batch x row patches x embedding_dim)",
    )

    mean_column_pooled_embeddings: Optional[List[List[List[float]]]] = Field(
        default=None,
        description="Mean column pooled embeddings (batch x column patches x embedding_dim)",
    )
