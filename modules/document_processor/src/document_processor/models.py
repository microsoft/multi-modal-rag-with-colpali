# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Public indexing contract shared by processing, storage, and result reporting."""

import hashlib
import io
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import NAMESPACE_URL, uuid5

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field


def generate_page_id(document_id: str, page_number: int) -> str:
    return str(uuid5(NAMESPACE_URL, f"{document_id}:page:{page_number}"))


def compute_file_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def compute_page_hash(image: Image.Image) -> str:
    with io.BytesIO() as buffer:
        image.save(
            buffer,
            format="JPEG",
            quality=90,
            subsampling=0,
            optimize=False,
            progressive=False,
        )
        return f"jpeg1:{hashlib.sha256(buffer.getvalue()).hexdigest()}"


class DocumentSource(BaseModel):
    blob_url: str
    container: str
    path: str
    file_name: str
    file_extension: str

    @classmethod
    def from_url(cls, url: str) -> "DocumentSource":
        parsed = urlsplit(url)
        parts = unquote(parsed.path).lstrip("/").split("/", 1)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Expected an absolute blob URL")
        if len(parts) != 2 or not all(parts) or not parts[1].split("/")[-1]:
            raise ValueError("Blob URL must contain a container and a filename")
        name = parts[1].split("/")[-1]
        return cls(
            blob_url=parsed._replace(query="", fragment="").geturl(),
            container=parts[0],
            path=parts[1],
            file_name=name,
            file_extension=f".{name.rsplit('.', 1)[1].lower()}" if "." in name else "",
        )

    @property
    def document_id(self) -> str:
        return self.blob_url


class DocumentMetadata(BaseModel):
    document: dict[str, Any] = Field(default_factory=dict)
    blob: dict[str, Any] = Field(default_factory=dict)


class DocumentPage(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    filename: str
    page_number: int = 1
    text_content: str | None = None
    image_content: Image.Image
    metadata: dict[str, Any] = Field(default_factory=dict)
    effective_dpi: int = 72


class ProcessedPage(BaseModel):
    source: DocumentSource
    metadata: DocumentMetadata = Field(default_factory=DocumentMetadata)
    page_number: int
    text_content: str | None = None
    page_image_url: str | None = None
    embeddings: dict[str, list[list[float]]] = Field(default_factory=dict)
    file_hash: str | None = None
    indexed_with_file_hash: str | None = None
    page_hash: str | None = None
    effective_dpi: int | None = None
    processing_fingerprint: str
    page_count: int
    indexed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def document_id(self) -> str:
        return self.source.document_id

    @property
    def page_id(self) -> str:
        return generate_page_id(self.document_id, self.page_number)


class BlobEvent(BaseModel):
    blob_url: str = Field(alias="url")
    blob_type: str | None = Field(default=None, alias="blobType")
    content_type: str | None = Field(default=None, alias="contentType")
    content_length: int | None = Field(default=None, alias="contentLength")


class ServiceBusEvent(BaseModel):
    event_type: str
    data: BlobEvent
    force_reindex: bool = False

    @classmethod
    def from_message_body(cls, body: dict[str, Any]) -> "ServiceBusEvent":
        return cls(
            event_type=body["eventType"],
            data=BlobEvent(**body["data"]),
            force_reindex=body.get("force_reindex", False),
        )


class ProcessingResult(BaseModel):
    success: bool
    document_id: str
    indexed_count: int = 0
    page_count: int = 0
    skipped_pages: int = 0
    skip_reason: str | None = None
    error_message: str | None = None
    processing_time_seconds: float = 0
    permanent_failure: bool = False
    dead_letter_reason: str | None = None

    @property
    def success_rate(self) -> float:
        return 100 * self.indexed_count / self.page_count if self.page_count else 0


class EmbedHealthResponse(BaseModel):
    status: str
    model_loaded: bool = False
    model_info: dict[str, Any] | None = None
    message: str | None = None
    error: str | None = None


class PoolingType(str, Enum):
    NONE = "none"
    HIERARCHICAL_POOLING = "hierarchical_pooling"
    MEAN_POOLING = "mean_pooling"


class EmbedRequest(BaseModel):
    images: list[str]
    pooling_type: list[PoolingType]
    pooling_config: dict[str, Any] = Field(default_factory=lambda: {"pool_factor": 3})
    priority: int | None = None
