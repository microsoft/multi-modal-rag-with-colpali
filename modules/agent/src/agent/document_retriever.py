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
import base64
import logging
import math
import os
from typing import Dict, List, Optional
from urllib.parse import unquote, urlparse

import aiohttp
from azure.identity.aio import DefaultAzureCredential
from azure.storage.blob.aio import BlobServiceClient
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Prefetch, QuantizationSearchParams, SearchParams
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
)

from .models import DocumentChunk

RRF_RANK_CONSTANT = 60
EXPANDED_QUERY_RESULT_LIMIT = 25


class DocumentRetriever:
    """
    Tool for retrieving documents using ColQwen embeddings and Qdrant search.

    This class provides the search_documents function that can be registered
    as a tool with the Azure OpenAI Agent.
    """

    def __init__(
        self, fetch_images: bool = True, collection_name: Optional[str] = None
    ):
        """
        Initialize the document retriever.

        Args:
            fetch_images: If True, automatically fetch and base64-encode images from blob storage URLs.
                         If False, only return page_image_url without fetching. Default is True.
            collection_name: Optional Qdrant collection name to use. If not provided, uses QDRANT_COLLECTION_NAME
                           environment variable or defaults to "colpali-documents".

        Uses Kubernetes service DNS to connect to ColQwen inference and Qdrant services.
        No authentication required for in-cluster service-to-service communication.
        """
        self.fetch_images = fetch_images

        # ColQwen inference endpoint (Kubernetes service)
        self.endpoint_url = os.getenv("COLPALI_INFERENCE_ENDPOINT")

        # Qdrant vector database endpoint (Kubernetes service)
        self.qdrant_endpoint = os.getenv(
            "QDRANT_ENDPOINT", "http://colpali-stack-qdrant:6333"
        )
        self.collection_name = collection_name or os.getenv(
            "QDRANT_COLLECTION_NAME", "colpali-documents"
        )
        self.request_timeout = int(os.getenv("COLPALI_REQUEST_TIMEOUT", "120"))

        # Qdrant API key (from Key Vault via CSI driver)
        self.qdrant_api_key = os.getenv("QDRANT_API_KEY")
        if not self.endpoint_url:
            raise ValueError("COLPALI_INFERENCE_ENDPOINT not set")

        if not self.qdrant_endpoint:
            raise ValueError("QDRANT_ENDPOINT not set")

        self.endpoint_url = f"{self.endpoint_url}/embeddings"

        # Initialize Azure Blob client for fetching images
        self.credential = DefaultAzureCredential(require_envvar=True)

        self.blob_service_client = None  # Initialized on first use

        # Initialize Qdrant async client
        try:
            self.qdrant_client = AsyncQdrantClient(
                url=self.qdrant_endpoint,
                api_key=self.qdrant_api_key,
                timeout=120,
            )
            logging.info(
                "DocumentRetriever initialized with AsyncQdrant: %s",
                self.qdrant_endpoint,
            )
        except Exception as e:
            logging.error("Failed to initialize Qdrant async client: %s", str(e))
            raise

    def _get_headers(self) -> Dict[str, str]:
        return {"Content-Type": "application/json", "Accept": "application/json"}

    @staticmethod
    def _return_none_on_error(retry_state: RetryCallState) -> None:
        return None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(0.5),
        retry=retry_if_exception_type(Exception),
        retry_error_callback=_return_none_on_error,
    )
    async def _fetch_image_from_url(self, image_url: str) -> Optional[str]:
        """
        Fetch image from Azure Blob Storage URL and convert to base64.
        Uses Azure SDK with managed identity authentication.

        Args:
            image_url: Full Azure Blob Storage URL for the page image

        Returns:
            Base64 encoded image string, or None if fetch fails
        """
        try:
            # Parse the blob URL to extract account name, container, and blob name
            parsed_url = urlparse(image_url)
            # URL format: https://<account>.blob.core.windows.net/<container>/<blob>
            path_parts = parsed_url.path.lstrip("/").split("/", 1)
            if len(path_parts) != 2:
                logging.error("Invalid blob URL format: %s", image_url)
                return None

            container_name, blob_name = (unquote(part) for part in path_parts)
            account_url = f"{parsed_url.scheme}://{parsed_url.netloc}"

            # Initialize blob service client on first use
            if self.blob_service_client is None:
                self.blob_service_client = BlobServiceClient(
                    account_url=account_url, credential=self.credential
                )

            # Get blob client and download
            blob_client = self.blob_service_client.get_blob_client(
                container=container_name, blob=blob_name
            )

            # Download blob content
            download_stream = await blob_client.download_blob()
            image_data = await download_stream.readall()

            return base64.b64encode(image_data).decode("utf-8")

        except Exception as e:
            logging.warning("Failed to fetch image from %s: %s", image_url, str(e))
            raise

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(0.5),
        retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)),
        retry_error_callback=_return_none_on_error,
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
                            if not isinstance(embeddings, list) or len(
                                embeddings
                            ) != len(queries):
                                raise ValueError(
                                    "Embedding batch count mismatch: expected "
                                    f"{len(queries)} query embeddings"
                                )
                            if any(
                                not isinstance(embedding, list) or not embedding
                                for embedding in embeddings
                            ):
                                raise ValueError(
                                    "Embedding batch contains an empty query"
                                )
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
                        raise aiohttp.ClientError(f"Bad response: {response.status}")

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logging.error("Failed to embed queries: %s", str(e))
            raise
        except Exception as e:
            logging.error("Unexpected error embedding queries: %s", str(e))
            raise

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(0.5),
        retry=retry_if_exception_type(Exception),
    )
    async def _query_qdrant_with_retry(self, **kwargs):
        """
        Query Qdrant with retry logic for network failures.

        Args:
            **kwargs: All arguments to pass to qdrant_client.query_points()

        Returns:
            Query results from Qdrant
        """
        try:
            return await self.qdrant_client.query_points(**kwargs)
        except Exception as e:
            logging.error(
                "Qdrant query_points failed with %s: %s\nkwargs: %s",
                type(e).__name__,
                str(e),
                {k: type(v).__name__ for k, v in kwargs.items()},
            )
            import traceback

            logging.error("Full traceback:\n%s", traceback.format_exc())
            raise

    async def search_documents_batch(
        self,
        queries: List[str],
        top_k: Optional[int] = 5,
        collection_name: Optional[str] = None,
        query_filter: Optional[Dict] = None,
    ) -> List[DocumentChunk]:
        """
        Search for relevant documents using multiple queries processed in parallel through GPU.
        Fuses expanded-query rankings with RRF before final limiting.

        Uses the mean_pooling_with_hierarchical_quantized strategy:
        - Prefetch: Binary-quantized mean pooled vectors with 2x oversampling and float rescoring
        - Rerank: Exact search on hierarchical pooled vectors

        Args:
            queries: List of search query texts
            top_k: Final number of top unique results to return after deduplication
            collection_name: Optional Qdrant collection name to override the default
            query_filter: Optional dict filter to apply before vector search (e.g., filter by dataset)

        Returns:
            Deduplicated and limited list of DocumentChunk objects
        """
        try:
            # Ensure we always have a concrete integer for calculations/slicing
            if top_k is None:
                top_k = 5
            queries = list(dict.fromkeys(queries))
            result_limit = (
                max(EXPANDED_QUERY_RESULT_LIMIT, top_k) if len(queries) > 1 else top_k
            )

            # Use provided collection_name or fall back to instance default
            target_collection = collection_name or self.collection_name

            logging.info(
                "Searching %d queries (top_k=%d) in '%s'",
                len(queries),
                top_k,
                target_collection,
            )

            all_query_embeddings = await self._embed_queries(queries)
            if not all_query_embeddings:
                logging.error("Failed to generate batch query embeddings")
                return []

            async def search_single_query(
                query: str, query_embeddings: List[List[float]], query_index: int
            ):
                """Search Qdrant for a single query's embeddings."""
                try:
                    logging.debug(
                        "Processing query %d: %s (top_k=%d)",
                        query_index,
                        query,
                        top_k,
                    )

                    # Two-stage retrieval: mean_pooling_with_hierarchical_quantized strategy
                    prefetch_limit = max(200, top_k)

                    # Prefetch with binary-quantized mean pooled vectors, rerank with hierarchical for accuracy
                    quantized_prefetch_params = SearchParams(
                        hnsw_ef=64,
                        exact=False,
                        indexed_only=True,
                        quantization=QuantizationSearchParams(
                            ignore=False,
                            rescore=True,
                            oversampling=2.0,
                        ),
                    )
                    query_kwargs = {
                        "collection_name": target_collection,
                        "query": query_embeddings,
                        "prefetch": [
                            Prefetch(
                                query=query_embeddings,
                                limit=prefetch_limit,
                                using="mean_column_pooled",
                                params=quantized_prefetch_params,
                            ),
                            Prefetch(
                                query=query_embeddings,
                                limit=prefetch_limit,
                                using="mean_row_pooled",
                                params=quantized_prefetch_params,
                            ),
                        ],
                        "using": "hierarchical_pooled",
                        "limit": result_limit,
                        "with_payload": True,
                    }
                    if query_filter is not None:
                        query_kwargs["query_filter"] = query_filter

                    search_results = await self._query_qdrant_with_retry(**query_kwargs)
                    chunks = []

                    for result in search_results.points:
                        payload = result.payload or {}
                        source = payload.get("source", {})
                        metadata = payload.get("metadata", {})
                        document = metadata.get("document", {})
                        blob = metadata.get("blob", {})
                        page_image_url = source.get("page_image_url")
                        text_content = payload.get("text_content")

                        chunk = DocumentChunk(
                            document_id=payload.get("document_id"),
                            source_file=document.get("filename", "unknown"),
                            page_number=payload.get("page_number", 0),
                            page_image_url=page_image_url,
                            page_image_base64=None,
                            text_content=text_content,
                            score=result.score,
                            corpus_id=blob.get("corpus_id"),
                            doc_id=blob.get("doc_id"),
                            source=blob.get("source"),
                            metadata=payload,
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

            unique_chunks: Dict[tuple[str, int], DocumentChunk] = {}
            rank_contributions: Dict[tuple[str, int], List[float]] = {}

            for i, search_result in enumerate(all_search_results):
                if isinstance(search_result, Exception):
                    logging.error(
                        "Search task %d failed with exception: %s", i, search_result
                    )
                    continue

                seen_pages = set()
                for rank, chunk in enumerate(search_result, start=1):
                    doc_key = chunk.page_key
                    if doc_key in seen_pages:
                        continue
                    seen_pages.add(doc_key)
                    unique_chunks.setdefault(doc_key, chunk)
                    rank_contributions.setdefault(doc_key, []).append(
                        1.0 / (RRF_RANK_CONSTANT + rank)
                    )

            all_chunks = list(unique_chunks.values())
            if len(queries) > 1:
                for chunk in all_chunks:
                    chunk.score = math.fsum(rank_contributions[chunk.page_key])
                all_chunks.sort(key=lambda chunk: (-chunk.score, chunk.page_key))
            final_chunks = all_chunks[:top_k]

            # Fetch only final pages, not every candidate from every rewrite.
            if self.fetch_images:
                image_chunks = [chunk for chunk in final_chunks if chunk.page_image_url]
                image_results = await asyncio.gather(
                    *(
                        self._fetch_image_from_url(chunk.page_image_url)
                        for chunk in image_chunks
                        if chunk.page_image_url is not None
                    ),
                    return_exceptions=True,
                )
                for chunk, image_result in zip(image_chunks, image_results):
                    if isinstance(image_result, Exception):
                        logging.warning(
                            "Failed to fetch image for %s page %d: %s",
                            chunk.source_file,
                            chunk.page_number,
                            image_result,
                        )
                    elif image_result is not None:
                        chunk.page_image_base64 = image_result
                    if not chunk.page_image_base64 and chunk.text_content:
                        logging.warning(
                            "Failed to fetch image for %s page %d, will use text content as fallback",
                            chunk.source_file,
                            chunk.page_number,
                        )

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

    async def search_documents(
        self, query: str, top_k: int = 5, collection_name: Optional[str] = None
    ) -> List[DocumentChunk]:
        """
        Search for relevant documents using ColQwen embeddings and Qdrant.

        This method now uses the batch processing internally for consistency.

        Args:
            query: The search query text
            top_k: Number of top results to return (default: 5)
            collection_name: Optional Qdrant collection name to override the default

        Returns:
            List of DocumentChunk objects with source file, page number, and relevance score
        """
        # Use batch processing for single query for consistency and efficiency
        return await self.search_documents_batch(
            [query], top_k=top_k, collection_name=collection_name
        )
