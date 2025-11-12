# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""
Pydantic data models for document processing pipeline.
Replaces dictionary-based data passing with strongly-typed models.
"""

import base64
import hashlib
import logging
import uuid
from datetime import datetime
from enum import Enum
from io import BytesIO
from typing import Any, Dict, List, Optional, Union

from PIL import Image
from PIL.ImageFile import ImageFile
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class DocumentPage(BaseModel):
    """Represents a single page from a processed document."""

    page_number: int = Field(..., description="Page number (1-based)")
    text_content: str = Field(default="", description="Extracted text from the page")
    image_content: Union[Image.Image, ImageFile, bytes] = Field(
        ..., description="Single page image"
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Document metadata (PDF metadata, custom fields, etc.)",
    )

    class Config:
        arbitrary_types_allowed = True


class ProcessedPage(BaseModel):
    """Processed page that matches exactly what we need for Qdrant points."""

    document_id: str = Field(..., description="Document identifier")
    page_number: int = Field(..., description="Page number within document")
    text_content: str = Field(default="", description="Page text content")
    image_content: str = Field(default="", description="Base64-encoded page image")
    filename: str = Field(..., description="Original filename of the document")
    file_extension: str = Field(..., description="File extension (e.g., '.pdf')")
    blob_url: Optional[str] = Field(
        None, description="Full blob URL including storage account"
    )
    embeddings: Dict[str, List[List[float]]] = Field(
        default_factory=dict, description="Dictionary of embedding vectors by type"
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Document and blob metadata (PDF metadata, blob properties, custom fields, etc.)",
    )
    indexed_at: datetime = Field(
        default_factory=datetime.utcnow, description="Indexing timestamp"
    )

    @property
    def page_id(self) -> str:
        """Generate consistent UUID for QDRANT indexing."""
        page_string = f"{self.document_id}_page_{self.page_number}"
        hash_bytes = hashlib.sha256(page_string.encode("utf-8")).digest()[:16]
        return str(uuid.UUID(bytes=hash_bytes))

    @classmethod
    def from_document_page(
        cls,
        document_page: "DocumentPage",
        document_id: str,
        filename: str,
        file_extension: str,
        blob_url: Optional[str] = None,
        additional_metadata: Optional[Dict[str, Any]] = None,
    ) -> "ProcessedPage":
        """Create ProcessedPage from DocumentPage."""
        # Convert image to base64
        image_b64 = ""
        if isinstance(document_page.image_content, (Image.Image, ImageFile)):
            buffer = BytesIO()
            document_page.image_content.save(buffer, format="PNG")
            image_b64 = base64.b64encode(buffer.getvalue()).decode()
        elif isinstance(document_page.image_content, bytes):
            image_b64 = base64.b64encode(document_page.image_content).decode()

        # Merge document page metadata with additional metadata
        merged_metadata = document_page.metadata.copy()
        if additional_metadata:
            merged_metadata.update(additional_metadata)

        return cls(
            document_id=document_id,
            page_number=document_page.page_number,
            text_content=document_page.text_content,
            image_content=image_b64,
            filename=filename,
            file_extension=file_extension,
            blob_url=blob_url,
            embeddings={},  # Will be filled after embedding generation
            metadata=merged_metadata,
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


class PoolingType(str, Enum):
    """Enumeration for different pooling types."""

    NONE = "none"
    HIERARCHICAL_POOLING = "hierarchical_pooling"
    MEAN_POOLING = "mean_pooling"


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
