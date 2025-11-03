"""
QDRANT search indexer for ColPali/ColQwen2 embeddings.
Supports QDRANT with optimized pooling strategies.
Based on research from https://qdrant.tech/documentation/advanced-tutorials/pdf-retrieval-at-scale/

Environment Variables:
- ENABLE_QDRANT: Set to "true" to enable QDRANT indexing
- QDRANT_URL: QDRANT service URL
"""

import base64
import hashlib
import logging
import os
import uuid
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, List, Optional

from azure.identity.aio import DefaultAzureCredential
from PIL import Image
from qdrant_client import QdrantClient, models


class SearchIndexer:
    """
    QDRANT search indexer supporting ColPali/ColQwen2 embeddings.

    Uses hierarchical pooling embeddings provided by the ColPali API:
    - Original embeddings: Full token vectors for precise reranking
    - Pooled embeddings: Compressed representation from HierarchicalTokenPooler for fast first-stage retrieval

    QDRANT schema: Multi-vector configuration with original and pooled embeddings
    """

    def __init__(self, credential: Optional[DefaultAzureCredential] = None):
        """Initialize QDRANT client based on environment configuration."""
        self.qdrant_client = None
        self.credential = credential or DefaultAzureCredential()

        # Initialize QDRANT if enabled
        self._init_qdrant()

    def _init_qdrant(self):
        """Initialize QDRANT client."""
        endpoint = os.getenv("QDRANT_ENDPOINT")
        if not endpoint:
            logging.error("QDRANT_ENDPOINT not set - QDRANT indexing disabled")
            return

        try:
            self.qdrant_client = QdrantClient(url=endpoint, port=443)
            logging.info(f"QDRANT client initialized: {endpoint}")
        except Exception as e:
            logging.error(f"Failed to initialize QDRANT: {e}")

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
                qdrant_success = self._index_to_qdrant(embeddings_results)
                if qdrant_success:
                    logging.info("QDRANT indexing completed successfully")
                    return True
                else:
                    logging.error("QDRANT indexing failed")
                    return False
            except Exception as e:
                logging.error(f"QDRANT indexing error: {e}")
                return False

        return False

    def _index_to_qdrant(self, embeddings_results: List[Dict[str, Any]]) -> bool:
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
                self.qdrant_client.upsert(
                    collection_name="colpali-documents", points=points
                )
            else:
                # Multiple pages - use batching
                batch_size = 10
                for i in range(0, len(points), batch_size):
                    batch = points[i : i + batch_size]
                    self.qdrant_client.upsert(
                        collection_name="colpali-documents", points=batch
                    )

            logging.info(f"QDRANT: {len(points)} points indexed successfully")
            return True

        except Exception as e:
            logging.error(f"QDRANT indexing failed: {e}")
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

            # QDRANT point with multi-vector configuration - store patches as-is
            point = models.PointStruct(
                id=str(uuid.uuid4()),
                vector={
                    "original": original_embeddings,  # Keep as list of patches
                    "pooled": pooled_embeddings,  # Keep as list of patches
                },
                payload={
                    "id": self._generate_document_id(source_file, page_num),
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
        """Generate a unique document ID."""
        hash_input = f"{source_file}_page_{page_number}"
        doc_hash = hashlib.sha256(hash_input.encode()).hexdigest()[:16]
        return f"doc_{doc_hash}"
