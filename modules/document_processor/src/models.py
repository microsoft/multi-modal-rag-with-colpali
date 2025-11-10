# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""
Pydantic data models for document processing pipeline.
Replaces dictionary-based data passing with strongly-typed models.
"""

import base64
import logging
from datetime import datetime
from io import BytesIO
from typing import Any, Dict, List, Optional, Sequence, Union

from PIL import Image
from PIL.ImageFile import ImageFile
from pydantic import BaseModel, Field, validator

logger = logging.getLogger(__name__)


class DocumentPage(BaseModel):
    """Represents a single page from a processed document."""

    page_number: int = Field(..., description="Page number (1-based)")
    text_content: str = Field(default="", description="Extracted text from the page")
    images: Sequence[Union[Image.Image, ImageFile, bytes]] = Field(
        default_factory=list, description="Page images"
    )

    class Config:
        arbitrary_types_allowed = True

    @validator("page_number")
    def validate_page_number(cls, v):
        if v < 1:
            raise ValueError("Page number must be >= 1")
        return v


class EmbeddingData(BaseModel):
    """Container for ColPali/ColQwen2 embeddings with multiple pooling types."""

    embeddings: Union[List[List[float]], Dict[str, List[List[float]]]] = Field(
        default_factory=list,
        description="Embedding vectors - either single list or dict by pooling type",
    )
    num_patches: int = Field(default=0, description="Number of embedding patches")
    patch_dimensions: int = Field(default=0, description="Dimensionality of each patch")


class ProcessedPage(BaseModel):
    """Complete processed page with embeddings ready for indexing."""

    document_id: str = Field(..., description="Unique document identifier")
    page_number: int = Field(..., description="Page number within document (1-based)")
    text_content: str = Field(default="", description="Extracted text content")
    images: Sequence[Union[Image.Image, ImageFile, bytes]] = Field(
        default_factory=list, description="Page images"
    )
    embeddings: EmbeddingData = Field(
        default_factory=EmbeddingData, description="Generated embeddings"
    )
    source_file: str = Field(..., description="Original source filename")

    class Config:
        arbitrary_types_allowed = True

    @property
    def page_id(self) -> str:
        """Generate consistent page ID for QDRANT indexing."""
        return f"{self.document_id}_page_{self.page_number}"

    def to_base64_images(self) -> List[str]:
        """Convert images to base64 strings for serialization."""
        base64_images = []
        for img in self.images:
            if isinstance(img, (Image.Image, ImageFile)):
                buffer = BytesIO()
                img.save(buffer, format="PNG")
                image_base64 = base64.b64encode(buffer.getvalue()).decode()
            else:
                image_base64 = base64.b64encode(img).decode()
            base64_images.append(image_base64)
        return base64_images


class QdrantPoint(BaseModel):
    """QDRANT point structure for vector database operations with multiple embedding types."""

    page_id: str = Field(..., description="Unique page identifier")
    document_id: str = Field(..., description="Document identifier")
    page_number: int = Field(..., description="Page number within document")
    embeddings_dict: Dict[str, List[List[float]]] = Field(
        ...,
        description="Dictionary of embedding vectors by type (original, mean_pooling_rows, mean_pooling_columns)",
    )
    text_content: str = Field(default="", description="Page text content")
    images_base64: List[str] = Field(
        default_factory=list, description="Base64-encoded images"
    )
    indexed_at: datetime = Field(
        default_factory=lambda: datetime.utcnow(), description="Indexing timestamp"
    )

    @classmethod
    def from_processed_page(cls, page: ProcessedPage) -> "QdrantPoint":
        """Create QdrantPoint from ProcessedPage."""
        # Handle both old and new embedding formats
        if hasattr(page.embeddings, "embeddings") and isinstance(
            page.embeddings.embeddings, list
        ):
            # Old format - single embedding list
            embeddings_dict = {"original": page.embeddings.embeddings}
        elif hasattr(page.embeddings, "embeddings") and isinstance(
            page.embeddings.embeddings, dict
        ):
            # New format - dictionary of embedding types
            embeddings_dict = page.embeddings.embeddings
        else:
            # Fallback
            embeddings_dict = {"original": []}

        return cls(
            page_id=page.page_id,
            document_id=page.document_id,
            page_number=page.page_number,
            embeddings_dict=embeddings_dict,
            text_content=page.text_content,
            images_base64=page.to_base64_images(),
        )


class SearchResult(BaseModel):
    """Search result from QDRANT with score and metadata."""

    page_id: str = Field(..., description="Unique page identifier")
    score: float = Field(..., description="Similarity score")
    document_id: str = Field(..., description="Document identifier")
    page_number: int = Field(..., description="Page number within document")
    text_content: str = Field(default="", description="Page text content")
    images_base64: List[str] = Field(
        default_factory=list, description="Base64-encoded images"
    )
    indexed_at: Optional[str] = Field(None, description="Indexing timestamp")
    original_embeddings: Optional[List[List[float]]] = Field(
        None, description="Original embeddings for reranking"
    )


class CollectionInfo(BaseModel):
    """QDRANT collection information."""

    name: str = Field(..., description="Collection name")
    status: str = Field(..., description="Collection status")
    points_count: int = Field(..., description="Number of points in collection")
    segments_count: int = Field(..., description="Number of segments")
    vectors_config: Dict[str, Any] = Field(
        default_factory=dict, description="Vector configuration"
    )


class BlobEvent(BaseModel):
    """Azure Blob Storage event data."""

    blob_url: str = Field(..., alias="url", description="Full URL of the blob")
    blob_type: str = Field(
        ..., alias="blobType", description="Type of blob (e.g., BlockBlob)"
    )
    content_type: str = Field(
        ..., alias="contentType", description="MIME type of the blob"
    )
    content_length: Optional[int] = Field(
        None, alias="contentLength", description="Size of the blob in bytes"
    )

    @property
    def blob_name(self) -> str:
        """Extract blob name from URL."""
        return self.blob_url.split("/")[-1]

    @property
    def document_id(self) -> str:
        """Generate document ID from blob name."""
        return self.blob_name.replace(".pdf", "")

    @property
    def is_pdf(self) -> bool:
        """Check if blob is a PDF file."""
        return (
            self.content_type == "application/pdf"
            or self.blob_name.lower().endswith(".pdf")
        )


class ServiceBusEvent(BaseModel):
    """Service Bus message event wrapper."""

    event_type: str = Field(..., description="Type of event")
    subject: str = Field(..., description="Event subject")
    data: BlobEvent = Field(..., description="Event data payload")
    event_time: datetime = Field(..., description="Event timestamp")

    @classmethod
    def from_message_body(cls, body: Dict[str, Any]) -> "ServiceBusEvent":
        """Parse Service Bus message body into structured event."""

        # Handle Event Grid format (expected format)
        if "eventType" in body and "data" in body:
            return cls(
                event_type=body.get("eventType", ""),
                subject=body.get("subject", ""),
                data=BlobEvent(**body.get("data", {})),
                event_time=datetime.fromisoformat(
                    body.get("eventTime", "").replace("Z", "+00:00")
                ),
            )
        else:
            raise ValueError(f"Unknown message format: {body}")


class ProcessingResult(BaseModel):
    """Result of document processing operation."""

    success: bool = Field(..., description="Whether processing succeeded")
    document_id: str = Field(..., description="Document identifier")
    pages_processed: int = Field(
        default=0, description="Number of pages successfully processed"
    )
    total_pages: int = Field(default=0, description="Total number of pages in document")
    error_message: Optional[str] = Field(
        None, description="Error message if processing failed"
    )
    processing_time_seconds: Optional[float] = Field(
        None, description="Time taken to process"
    )

    @property
    def success_rate(self) -> float:
        """Calculate success rate as percentage."""
        if self.total_pages == 0:
            return 0.0
        return (self.pages_processed / self.total_pages) * 100.0


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
