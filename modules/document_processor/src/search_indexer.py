"""
QDRANT search indexer for ColPali/ColQwen2 embeddings.
Supports QDRANT with optimized pooling strategies and async I/O operations.
Based on research from https://qdrant.tech/documentation/advanced-tutorials/pdf-retrieval-at-scale/

Environment Variables:
- QDRANT_ENDPOINT: QDRANT service ENDPOINT
- QDRANT_COLLECTION_NAME: Collection name (default: colpali-documents)
"""

import base64
import hashlib
import logging
import os
import uuid
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, List, Optional

from azure.identity.aio import DefaultAzureCredential, ManagedIdentityCredential
from PIL import Image
from qdrant_client import AsyncQdrantClient, models


class SearchIndexer:
    """
    QDRANT search indexer supporting ColPali/ColQwen2 embeddings.

    Uses async I/O patterns with AsyncQdrantClient for non-blocking operations.

    Uses hierarchical pooling embeddings provided by the ColPali API:
    - Original embeddings: Full token vectors for precise reranking
    - Pooled embeddings: Compressed representation from HierarchicalTokenPooler for fast first-stage retrieval

    QDRANT schema: Multi-vector configuration with original and pooled embeddings
    """

    def __init__(
        self,
        credential: Optional[DefaultAzureCredential | ManagedIdentityCredential] = None,
    ):
        """Initialize QDRANT client based on environment configuration."""
        self.qdrant_client = None
        self.credential = credential or DefaultAzureCredential()
        self.collection_name = os.getenv("QDRANT_COLLECTION_NAME", "colpali-documents")
        self.qdrant_endpoint = os.getenv("QDRANT_ENDPOINT")

        # Initialize QDRANT if enabled
        if self.qdrant_endpoint:
            try:
                self.qdrant_client = AsyncQdrantClient(
                    url=self.qdrant_endpoint, port=443
                )
                logging.info(f"Async QDRANT client initialized: {self.qdrant_endpoint}")
            except Exception as e:
                logging.error(f"Failed to initialize QDRANT client: {e}")
        else:
            logging.error("QDRANT_ENDPOINT not set - QDRANT indexing disabled")

    async def index_embeddings(self, embeddings_results: List[Dict[str, Any]]) -> bool:
        """
        Index embeddings into QDRANT.

        Args:
            embeddings_results: List of embedding results with metadata

        Returns:
            True if indexing successful, False otherwise
        """

        if not embeddings_results:
            logging.warning("No embeddings to index")
            return True

        if self.qdrant_client:
            try:
                qdrant_success = await self._index_to_qdrant(embeddings_results)
                if qdrant_success:
                    logging.info("QDRANT indexing completed successfully")
                    return True
                else:
                    logging.error(
                        f"QDRANT indexing failed for {len(embeddings_results)} results - check QDRANT connection and collection configuration"
                    )
                    return False
            except Exception as e:
                logging.error(
                    f"QDRANT indexing error for {len(embeddings_results)} results: {type(e).__name__}: {e}"
                )
                return False

        return False

    async def delete_document_pages(self, source_file: str) -> bool:
        """
        Delete all pages for a given source file from QDRANT.

        This is used before re-indexing a document to ensure that if the new version
        has fewer pages than the old version, orphaned pages are removed.

        Args:
            source_file: The source filename to delete all pages for

        Returns:
            True if deletion successful or no points found, False otherwise
        """
        if not self.qdrant_client:
            logging.error("QDRANT client not initialized")
            return False

        try:
            # Delete all points where payload.source_file matches the given filename
            await self.qdrant_client.delete(
                collection_name=self.collection_name,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="source_file",
                                match=models.MatchValue(value=source_file),
                            )
                        ]
                    )
                ),
            )
            logging.info(f"QDRANT: Deleted existing pages for document: {source_file}")
            return True

        except Exception as e:
            logging.error(
                f"QDRANT: Failed to delete existing pages for {source_file}: {e}"
            )
            return False

    async def _index_to_qdrant(self, embeddings_results: List[Dict[str, Any]]) -> bool:
        """Index documents to QDRANT with multi-vector configuration."""
        if not self.qdrant_client:
            logging.error("QDRANT client not initialized")
            return False

        try:
            points = []
            for result in embeddings_results:
                point = self._transform_to_qdrant_point(result)
                if point:
                    points.append(point)

            if not points:
                return False

            # Upload to QDRANT - use single upload for individual pages, batching for larger sets
            if len(points) == 1:
                # Single page - upload immediately
                await self.qdrant_client.upsert(
                    collection_name=self.collection_name, points=points
                )
            else:
                # Multiple pages - use batching
                batch_size = 10
                for i in range(0, len(points), batch_size):
                    batch = points[i : i + batch_size]
                    await self.qdrant_client.upsert(
                        collection_name=self.collection_name, points=batch
                    )

            logging.info(f"QDRANT: {len(points)} points indexed successfully")
            return True

        except Exception as e:
            logging.error(
                f"QDRANT indexing failed while upserting {len(points)} points: {type(e).__name__}: {e}"
            )
            return False

    def _transform_to_qdrant_point(
        self, result: Dict[str, Any]
    ) -> Optional[models.PointStruct]:
        """Transform to QDRANT point with multi-vector configuration using API-provided embeddings."""
        try:
            page_content = result.get("page_content", {})
            embeddings_data = result.get("embeddings", {})
            source_file = result.get("source_file", "unknown")
            page_num = result.get("page_id", 0)

            if not embeddings_data or not isinstance(embeddings_data, dict):
                return None

            # Extract original and pooled embeddings from API response
            original_embeddings = embeddings_data.get("original_embeddings", [])
            pooled_embeddings = embeddings_data.get("pooled_embeddings", [])

            if not original_embeddings:
                return None

            # Use pooled embeddings if available, otherwise fall back to original embeddings
            if not pooled_embeddings:
                pooled_embeddings = original_embeddings
                logging.debug(
                    "No pooled embeddings available, using original embeddings for both vectors"
                )

            # Skip validation for performance - assume embeddings are in correct format

            page_image = page_content.get("page_image")
            page_image_base64 = (
                self._image_to_base64(page_image) if page_image else None
            )

            # Generate deterministic document ID to prevent duplicates on re-indexing
            # Using the same ID for the same document/page ensures upsert will update
            # existing entries rather than creating duplicates
            document_id = self._generate_document_id(source_file, page_num)

            # QDRANT point with multi-vector configuration - store patches as-is
            point = models.PointStruct(
                id=document_id,  # Use deterministic ID to avoid duplicates
                vector={
                    "original": original_embeddings,  # Keep as list of patches
                    "pooled": pooled_embeddings,  # Keep as list of patches
                },
                payload={
                    "id": document_id,  # Also store in payload for backward compatibility
                    "source_file": source_file,
                    "page_number": page_num,
                    "page_image_base64": page_image_base64,
                    "upload_timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )

            return point

        except Exception as e:
            logging.error(f"QDRANT transform failed: {e}")
            return None

    def _image_to_base64(self, image: Image.Image) -> str:
        """Convert PIL Image to base64 string for storage."""
        try:
            if not image:
                return ""

            if image.mode != "RGB":
                image = image.convert("RGB")

            buffer = BytesIO()
            image.save(buffer, format="JPEG", quality=85)
            return base64.b64encode(buffer.getvalue()).decode("utf-8")

        except Exception as e:
            logging.error(f"Failed to convert image to base64: {e}")
            return ""

    def _generate_document_id(self, source_file: str, page_number: int) -> str:
        """Generate a deterministic UUID from document metadata."""
        hash_input = f"{source_file}_page_{page_number}"
        doc_hash = hashlib.sha256(hash_input.encode()).hexdigest()
        # Create a deterministic UUID from the hash
        return str(uuid.UUID(doc_hash[:32]))
