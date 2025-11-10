"""
QDRANT search indexer for ColPali/ColQwen2 embeddings.
Supports QDRANT with optimized pooling strategies and async I/O operations.
Based on research from https://qdrant.tech/documentation/advanced-tutorials/pdf-retrieval-at-scale/

Environment Variables:
- QDRANT_ENDPOINT: QDRANT service ENDPOINT

Collection name is hardcoded as "colpali-documents".
"""

import logging
import os
from typing import Any, Dict, List, Optional

from azure.identity.aio import DefaultAzureCredential, ManagedIdentityCredential
from qdrant_client import AsyncQdrantClient, models

from .models import ProcessedPage, QdrantPoint, SearchResult


class QdrantIndex:
    """
    QDRANT search indexer supporting ColPali/ColQwen2 embeddings.

    Uses async I/O patterns with AsyncQdrantClient for non-blocking operations.
    Stores single embedding vectors for efficient retrieval.

    QDRANT schema: Single vector configuration for ColPali embeddings
    """

    def __init__(
        self,
        credential: Optional[DefaultAzureCredential | ManagedIdentityCredential] = None,
        require_endpoint: bool = True,
    ):
        """Initialize QDRANT client based on environment configuration."""
        self.qdrant_client = None
        self.credential = credential or DefaultAzureCredential()
        self.collection_name = "colpali-documents"  # Fixed collection name
        self.qdrant_endpoint = os.getenv("QDRANT_ENDPOINT")

        # QDRANT endpoint is required for production service
        if require_endpoint and not self.qdrant_endpoint:
            raise ValueError(
                "QDRANT_ENDPOINT environment variable is required but not set"
            )

        # Initialize QDRANT client only if endpoint is available
        if self.qdrant_endpoint:
            try:
                qdrant_api_key = os.getenv("QDRANT_API_KEY")

                self.qdrant_client = AsyncQdrantClient(
                    url=self.qdrant_endpoint,
                    timeout=120,
                    api_key=qdrant_api_key,
                )
                logging.info(f"Async QDRANT client initialized: {self.qdrant_endpoint}")
            except Exception as e:
                if require_endpoint:
                    logging.error(f"Failed to initialize QDRANT client: {e}")
                    raise ValueError(f"Failed to initialize QDRANT client: {e}")
                else:
                    logging.warning(
                        f"QDRANT client initialization skipped in local mode: {e}"
                    )
        else:
            logging.info("QDRANT client initialization skipped (local mode)")

    async def initialize(self) -> bool:
        """
        Initialize the search indexer, including ensuring the QDRANT collection exists.
        Call this method after creating the QdrantIndex instance.

        Returns:
            True if initialization successful, False otherwise
        """
        if self.qdrant_client:
            return await self._ensure_collection_exists()
        return True

    async def _ensure_collection_exists(self) -> bool:
        """
        Ensure the QDRANT collection exists with the correct schema.

        Returns:
            True if collection exists or was created successfully, False otherwise
        """
        try:
            collections_response = await self.qdrant_client.get_collections()  # type: ignore[union-attr]
            existing_collections = [c.name for c in collections_response.collections]

            if self.collection_name in existing_collections:
                logging.info(
                    f"QDRANT collection '{self.collection_name}' already exists"
                )
                return True

            # Create collection with pooled embedding configuration for ColPali embeddings
            # Mirror setup from: https://qdrant.tech/documentation/advanced-tutorials/pdf-retrieval-at-scale/
            await self.qdrant_client.create_collection(  # type: ignore[union-attr]
                collection_name=self.collection_name,
                vectors_config={
                    "original": models.VectorParams(  # switch off HNSW
                        size=128,
                        distance=models.Distance.COSINE,
                        multivector_config=models.MultiVectorConfig(
                            comparator=models.MultiVectorComparator.MAX_SIM
                        ),
                        hnsw_config=models.HnswConfigDiff(
                            m=0  # switching off HNSW
                        ),
                    ),
                    "mean_pooling_columns": models.VectorParams(
                        size=128,
                        distance=models.Distance.COSINE,
                        multivector_config=models.MultiVectorConfig(
                            comparator=models.MultiVectorComparator.MAX_SIM
                        ),
                    ),
                    "mean_pooling_rows": models.VectorParams(
                        size=128,
                        distance=models.Distance.COSINE,
                        multivector_config=models.MultiVectorConfig(
                            comparator=models.MultiVectorComparator.MAX_SIM
                        ),
                    ),
                },
                optimizers_config=models.OptimizersConfigDiff(
                    deleted_threshold=0.2,
                    vacuum_min_vector_number=1000,
                    default_segment_number=0,
                    flush_interval_sec=5,
                    max_optimization_threads=1,
                ),
                hnsw_config=models.HnswConfigDiff(
                    m=16,
                    ef_construct=100,
                    full_scan_threshold=10000,
                    max_indexing_threads=0,
                ),
            )

            logging.info(
                f"QDRANT collection '{self.collection_name}' created successfully"
            )
            return True

        except Exception as e:
            logging.error(f"Failed to ensure QDRANT collection exists: {e}")
            return False

    def _compute_page_id(self, document_id: str, page_number: int) -> str:
        """Generate consistent page ID."""
        return f"{document_id}_page_{page_number}"

    async def index_embeddings(self, processed_pages: List[ProcessedPage]) -> bool:
        """
        Index embeddings to QDRANT with single embedding structure.

        Args:
            processed_pages: List of ProcessedPage models with embeddings

        Returns:
            True if indexing successful, False otherwise
        """
        if not self.qdrant_client:
            logging.warning("QDRANT client not initialized - skipping indexing")
            return True

        try:
            points = []
            for processed_page in processed_pages:
                # Create QdrantPoint from ProcessedPage
                qdrant_point = QdrantPoint.from_processed_page(processed_page)

                # Validate embeddings exist
                if not qdrant_point.embeddings_dict:
                    logging.error(
                        f"Missing embeddings for page {qdrant_point.page_id}: "
                        f"embeddings_dict={qdrant_point.embeddings_dict}"
                    )
                    continue

                # Create QDRANT point structure with named vectors
                # Map embeddings to the collection's vector names
                vector_dict = {}

                # Map hierarchical to "original" vector
                if "original" in qdrant_point.embeddings_dict:
                    vector_dict["original"] = qdrant_point.embeddings_dict["original"]

                # Map mean pooling vectors
                if "mean_pooling_rows" in qdrant_point.embeddings_dict:
                    vector_dict["mean_pooling_rows"] = qdrant_point.embeddings_dict[
                        "mean_pooling_rows"
                    ]

                if "mean_pooling_columns" in qdrant_point.embeddings_dict:
                    vector_dict["mean_pooling_columns"] = qdrant_point.embeddings_dict[
                        "mean_pooling_columns"
                    ]

                if not vector_dict:
                    logging.error(
                        f"No valid embeddings found for page {qdrant_point.page_id}"
                    )
                    continue

                point = models.PointStruct(
                    id=qdrant_point.page_id,
                    vector=vector_dict,
                    payload={
                        "document_id": qdrant_point.document_id,
                        "page_number": qdrant_point.page_number,
                        "images": qdrant_point.images_base64,
                        "text_content": qdrant_point.text_content,
                        "indexed_at": qdrant_point.indexed_at.isoformat(),
                        "page_id": qdrant_point.page_id,
                    },
                )
                points.append(point)

            # Batch upsert to QDRANT
            if points:
                await self.qdrant_client.upsert(
                    collection_name=self.collection_name,
                    points=points,
                    wait=True,
                )
                logging.info(f"Indexed {len(points)} embeddings to QDRANT")

            return True

        except Exception as e:
            logging.error(f"Failed to index embeddings to QDRANT: {e}")
            return False

    async def delete_document_pages(self, document_id: str) -> bool:
        """
        Delete all pages for a document from QDRANT.

        Args:
            document_id: Document ID to delete pages for

        Returns:
            True if deletion successful, False otherwise
        """
        if not self.qdrant_client:
            logging.warning("QDRANT client not initialized - skipping deletion")
            return True

        try:
            # Delete by document_id filter
            await self.qdrant_client.delete(
                collection_name=self.collection_name,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="document_id",
                                match=models.MatchValue(value=document_id),
                            )
                        ]
                    )
                ),
                wait=True,
            )

            logging.info(f"Deleted pages for document '{document_id}' from QDRANT")
            return True

        except Exception as e:
            logging.error(f"Failed to delete pages for document '{document_id}': {e}")
            return False

    async def search_similar(
        self,
        query_embedding: List[float],
        limit: int = 10,
        score_threshold: float = 0.0,
        document_filter: Optional[str] = None,
        vector_name: str = "original",
    ) -> List[SearchResult]:
        """
        Search for similar embeddings using named vectors.

        Args:
            query_embedding: Query embedding vector
            limit: Maximum number of results to return
            score_threshold: Minimum similarity score threshold
            document_filter: Optional document ID to filter results
            vector_name: Name of the vector to search (original, mean_pooling_rows, mean_pooling_columns)

        Returns:
            List of search results with scores and metadata
        """
        if not self.qdrant_client:
            logging.warning("QDRANT client not initialized - returning empty results")
            return []

        try:
            # Build filter conditions
            filter_conditions = []
            if document_filter:
                filter_conditions.append(
                    models.FieldCondition(
                        key="document_id",
                        match=models.MatchValue(value=document_filter),
                    )
                )

            # Search using specific named vector
            search_results = await self.qdrant_client.search(
                collection_name=self.collection_name,
                query_vector=(vector_name, query_embedding),  # Named vector search
                query_filter=models.Filter(must=filter_conditions)
                if filter_conditions
                else None,
                limit=limit,
                score_threshold=score_threshold,
                with_payload=True,
                with_vectors=True,  # Return embeddings
            )

            # Format results
            results = []
            for hit in search_results:
                result = SearchResult(
                    page_id=str(hit.id),
                    score=float(hit.score),
                    document_id=hit.payload["document_id"] if hit.payload else "",  # type: ignore[index]
                    page_number=hit.payload["page_number"] if hit.payload else 0,  # type: ignore[index]
                    text_content=hit.payload.get("text_content", "")
                    if hit.payload
                    else "",  # type: ignore[union-attr]
                    images_base64=hit.payload.get("images", []) if hit.payload else [],  # type: ignore[union-attr]
                    indexed_at=hit.payload.get("indexed_at") if hit.payload else None,  # type: ignore[union-attr]
                )
                results.append(result)

            logging.info(f"Found {len(results)} similar embeddings")
            return results

        except Exception as e:
            logging.error(f"Failed to search similar embeddings: {e}")
            return []

    async def get_collection_info(self) -> Dict[str, Any]:
        """
        Get information about the QDRANT collection.

        Returns:
            Collection information dictionary
        """
        if not self.qdrant_client:
            return {"error": "QDRANT client not initialized"}

        try:
            collection_info = await self.qdrant_client.get_collection(
                self.collection_name
            )
            return {
                "name": self.collection_name,  # Use our collection name instead of config.name
                "status": str(collection_info.status),
                "points_count": collection_info.points_count or 0,
                "segments_count": getattr(collection_info, "segments_count", 0),
                "vectors_config": getattr(collection_info.config.params, "vectors", {}),  # type: ignore[attr-defined]
            }

        except Exception as e:
            logging.error(f"Failed to get collection info: {e}")
            return {"error": str(e)}

    async def close(self):
        """Close QDRANT client connection."""
        if self.qdrant_client:
            await self.qdrant_client.close()
            logging.info("QDRANT client connection closed")
