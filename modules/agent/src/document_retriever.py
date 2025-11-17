# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""
Document retrieval tool for Azure OpenAI Agent.

This tool embeds queries using the ColQwen inference service running in Kubernetes
and searches Qdrant vector database for relevant documents, returning results with
source file names and page numbers for citation.

Connects to in-cluster Kubernetes services:
- ColQwen Inference Service: http://colqwen-inference-service:80
- Qdrant Vector Database: http://qdrant:6333
"""

import asyncio
import logging
import os
from typing import Dict, List, Optional

import aiohttp
from pydantic import BaseModel, Field
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Prefetch
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


class DocumentChunk(BaseModel):
    """Represents a retrieved document chunk with citation information."""

    source_file: str = Field(..., description="Source file name")
    page_number: int = Field(..., description="Page number in the document")
    page_image_base64: Optional[str] = Field(
        None, description="Base64 encoded page image"
    )
    score: float = Field(0.0, description="Relevance score from vector search")


class DocumentRetriever:
    """
    Tool for retrieving documents using ColQwen embeddings and Qdrant search.

    This class provides the search_documents function that can be registered
    as a tool with the Azure OpenAI Agent.
    """

    def __init__(self):
        """
        Initialize the document retriever.

        Uses Kubernetes service DNS to connect to ColQwen inference and Qdrant services.
        No authentication required for in-cluster service-to-service communication.
        """
        # ColQwen inference endpoint (Kubernetes service)
        self.endpoint_url = os.getenv("COLQWEN_ENDPOINT")

        # Qdrant vector database endpoint (Kubernetes service)
        self.qdrant_endpoint = os.getenv(
            "QDRANT_ENDPOINT", "http://colpali-stack-qdrant:6333"
        )
        self.collection_name = os.getenv("QDRANT_COLLECTION_NAME", "colpali-documents")
        self.request_timeout = int(os.getenv("COLPALI_REQUEST_TIMEOUT", "120"))

        # Qdrant API key (from Key Vault via CSI driver)
        self.qdrant_api_key = os.getenv("QDRANT_API_KEY")
        if not self.endpoint_url:
            raise ValueError("COLQWEN_ENDPOINT not set")

        if not self.qdrant_endpoint:
            raise ValueError("QDRANT_ENDPOINT not set")

        # Ensure endpoint URL has correct format (should be /embeddings)
        if not self.endpoint_url.endswith("/embeddings"):
            self.endpoint_url = f"{self.endpoint_url.rstrip('/')}/embeddings"

        # Initialize Qdrant async client
        try:
            self.qdrant_client = AsyncQdrantClient(
                url=self.qdrant_endpoint, api_key=self.qdrant_api_key
            )
            logging.info(
                "DocumentRetriever initialized with AsyncQdrant: %s",
                self.qdrant_endpoint,
            )
        except Exception as e:
            logging.error("Failed to initialize Qdrant async client: %s", str(e))
            raise

    def _get_headers(self) -> Dict[str, str]:
        """Get HTTP headers for ColQwen service (no authentication needed for in-cluster services)."""
        return {"Content-Type": "application/json", "Accept": "application/json"}

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)),
        reraise=True,
    )
    async def _embed_queries(
        self, queries: List[str]
    ) -> Optional[List[List[List[float]]]]:
        """
        Embed multiple text queries using the ColQwen endpoint in a single batch.

        Args:
            queries: List of search query texts

        Returns:
            List of embeddings for each query, or None if embedding fails
        """
        try:
            logging.info("Batch embedding %d queries", len(queries))

            # Prepare payload for batch processing
            payload = {
                "texts": queries,
                "pooling_type": ["none"],  # Text queries only support 'none' pooling
            }

            headers = self._get_headers()

            timeout = aiohttp.ClientTimeout(total=self.request_timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    self.endpoint_url, json=payload, headers=headers
                ) as response:
                    if response.status == 200:
                        result = await response.json()

                        # Extract embeddings from response
                        # For batch text queries, embeddings is [batch x patches x dim]
                        if "embeddings" in result and result["embeddings"]:
                            embeddings = result["embeddings"]
                            logging.info(
                                "Received embeddings for %d queries", len(embeddings)
                            )
                            return embeddings

                        logging.error("Unexpected response format: %s", result)
                        return None
                    else:
                        text = await response.text()
                        logging.error(
                            "ColQwen endpoint returned status %s: %s",
                            response.status,
                            text,
                        )
                        return None

        except Exception as e:
            logging.error("Failed to embed queries: %s", str(e))
            return None

    async def _embed_query(self, query: str) -> Optional[List[List[float]]]:
        """
        Embed a single text query using the ColQwen endpoint.

        Args:
            query: The search query text

        Returns:
            Pooled embeddings for the query, or None if embedding fails
        """
        # Use batch embedding for single query for consistency
        batch_result = await self._embed_queries([query])
        if batch_result and len(batch_result) > 0:
            return batch_result[0]
        return None

    async def search_documents_batch(
        self, queries: List[str], top_k: int = 10
    ) -> List[DocumentChunk]:
        """
        Search for relevant documents using multiple queries processed in parallel through GPU.
        Handles deduplication and final limiting internally.

        Args:
            queries: List of search query texts
            top_k: Final number of top unique results to return after deduplication

        Returns:
            Deduplicated and limited list of DocumentChunk objects
        """
        try:
            # Ensure we always have a concrete integer for calculations/slicing
            if top_k is None:
                top_k = 10

            logging.info(
                "Batch searching documents for %d queries (final top_k=%d)",
                len(queries),
                top_k,
            )

            # Step 1: Batch embed all queries using GPU in parallel
            all_query_embeddings = await self._embed_queries(queries)

            if not all_query_embeddings:
                logging.error("Failed to generate batch query embeddings")
                return []

            logging.info(
                "Generated embeddings for %d queries", len(all_query_embeddings)
            )

            # Step 2: Create all Qdrant search tasks for parallel execution
            # Use higher per-query limit to ensure good results after deduplication
            per_query_limit = max(top_k * 2 // len(queries), 5)  # At least 5 per query

            async def search_single_query(
                query: str, query_embeddings: List[List[float]], query_index: int
            ):
                """Search Qdrant for a single query's embeddings."""
                try:
                    logging.debug(
                        "Processing query %d: %s (per_query_limit=%d)",
                        query_index,
                        query,
                        per_query_limit,
                    )

                    # Two-stage retrieval following Qdrant's recommended approach
                    prefetch_limit = per_query_limit * 10

                    # Use native async Qdrant client
                    search_results = await self.qdrant_client.query_points(
                        collection_name=self.collection_name,
                        query=query_embeddings,  # Multi-vector query embeddings
                        prefetch=[
                            Prefetch(
                                query=query_embeddings,
                                limit=prefetch_limit,
                                using="mean_pooled_columns",  # Fast first-stage retrieval - columns
                            ),
                            Prefetch(
                                query=query_embeddings,
                                limit=prefetch_limit,
                                using="mean_pooled_rows",  # Fast first-stage retrieval - rows
                            ),
                        ],
                        using="hierarchical_pooled",  # Precise reranking with hierarchical pooled vectors
                        limit=per_query_limit,
                        with_payload=True,
                    )

                    # Transform results into DocumentChunk objects
                    chunks = []
                    for result in search_results.points:
                        chunk = DocumentChunk(
                            source_file=result.payload.get("filename", "unknown"),
                            page_number=result.payload.get("page_number", 0),
                            page_image_base64=result.payload.get("image_content"),
                            score=result.score,
                        )
                        chunks.append(chunk)

                    logging.debug(
                        "Query %d returned %d chunks", query_index, len(chunks)
                    )
                    return chunks

                except Exception as e:
                    logging.error(
                        "Failed to search for query %d ('%s'): %s",
                        query_index,
                        query,
                        e,
                    )
                    return []

            # Run all Qdrant searches in parallel
            logging.info("Running %d Qdrant searches in parallel", len(queries))
            search_tasks = [
                search_single_query(query, query_embeddings, i)
                for i, (query, query_embeddings) in enumerate(
                    zip(queries, all_query_embeddings)
                )
            ]

            all_search_results = await asyncio.gather(
                *search_tasks, return_exceptions=True
            )

            # Step 3: Merge and deduplicate results
            all_chunks = []
            seen_docs = set()  # Track (filename, page_number) to avoid duplicates

            for i, search_result in enumerate(all_search_results):
                if isinstance(search_result, Exception):
                    logging.error(
                        "Search task %d failed with exception: %s", i, search_result
                    )
                    continue

                # Process chunks from this query
                for chunk in search_result:
                    doc_key = (chunk.source_file, chunk.page_number)

                    if doc_key not in seen_docs:
                        all_chunks.append(chunk)
                        seen_docs.add(doc_key)
                        logging.debug(
                            "Added unique chunk: %s (page %d, score=%.3f)",
                            chunk.source_file,
                            chunk.page_number,
                            chunk.score,
                        )
                    else:
                        logging.debug(
                            "Skipped duplicate chunk: %s (page %d)",
                            doc_key[0],
                            doc_key[1],
                        )

            # Sort by score (highest first) and limit to final top_k
            all_chunks.sort(key=lambda x: x.score, reverse=True)
            final_chunks = all_chunks[:top_k]

            logging.info(
                "Batch search completed: %d unique chunks from %d queries, returning top %d",
                len(all_chunks),
                len(queries),
                len(final_chunks),
            )
            return final_chunks

        except Exception as e:
            logging.error("Batch document search failed: %s", str(e))
            return []

    async def search_documents(self, query: str, top_k: int = 5) -> List[DocumentChunk]:
        """
        Search for relevant documents using ColQwen embeddings and Qdrant.

        This method now uses the batch processing internally for consistency.

        Args:
            query: The search query text
            top_k: Number of top results to return (default: 5)

        Returns:
            List of DocumentChunk objects with source file, page number, and relevance score
        """
        # Use batch processing for single query for consistency and efficiency
        return await self.search_documents_batch([query], top_k=top_k)
