# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""
ColQwen2 client for interfacing with Kubernetes ColQwen2 service.
"""

import asyncio
import base64
import logging
import os
from io import BytesIO
from typing import Any, Dict, List, Optional, Union

import aiohttp
from PIL import Image
from PIL.ImageFile import ImageFile
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .models import DocumentPage, EmbedHealthResponse, EmbedRequest


class ColPaliClient:
    """Client for communicating with ColQwen2 service in Kubernetes."""

    def __init__(
        self,
        require_endpoint: bool = True,
    ):
        # Configure for Kubernetes ColQwen2 service
        self.endpoint_url = os.getenv(
            "COLQWEN_SERVICE_URL", "http://colqwen-inference-service:8080"
        )
        self.request_timeout = int(os.getenv("COLPALI_REQUEST_TIMEOUT", "120"))
        self.max_image_size = int(os.getenv("COLPALI_MAX_IMAGE_SIZE", "1536"))

        # Configure concurrency limit for embedding requests
        max_concurrent_requests = int(os.getenv("COLPALI_MAX_CONCURRENT_REQUESTS", "5"))
        self.semaphore = asyncio.Semaphore(max_concurrent_requests)

        if require_endpoint and not self.endpoint_url:
            raise ValueError(
                "COLQWEN_SERVICE_URL environment variable is required but not set"
            )

        # Ensure endpoint URL has correct format for embedding endpoint
        if self.endpoint_url and not self.endpoint_url.endswith("/embeddings"):
            self.endpoint_url = f"{self.endpoint_url.rstrip('/')}/embeddings"

        logging.info(
            f"ColQwen2 client initialized for Kubernetes endpoint: {self.endpoint_url}"
        )
        logging.info(
            f"Concurrency limit: {max_concurrent_requests} concurrent requests"
        )

    async def health_check(self) -> Optional[EmbedHealthResponse]:
        """
        Check the health status of the ColQwen2 service.

        Returns:
            EmbedHealthResponse object or None if check failed
        """
        if not self.endpoint_url:
            return None

        try:
            health_url = self.endpoint_url.replace("/embeddings", "/health")
            headers = await self._get_auth_header()

            timeout = aiohttp.ClientTimeout(
                total=30
            )  # Shorter timeout for health check
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(health_url, headers=headers) as response:
                    if response.status == 200:
                        result = await response.json()
                        return EmbedHealthResponse(**result)
                    else:
                        logging.error(
                            f"Health check failed with status {response.status}"
                        )
                        return None
        except Exception as e:
            logging.error(f"Health check error: {str(e)}")
            return None

    async def _get_auth_header(self) -> Dict[str, str]:
        """Get headers for Kubernetes service."""
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)),
        reraise=True,
    )
    async def generate_embeddings(
        self, document_pages: Union[DocumentPage, List[DocumentPage]]
    ) -> Optional[Dict[str, Any]]:
        """
        Generate embeddings for document page(s) using ColQwen2 endpoint.

        Args:
            document_pages: Single DocumentPage or list of DocumentPages containing images and text

        Returns:
            Dictionary with embeddings, or None if generation failed.

            For single page input:
            {
                "embeddings": [[patch1, patch2, ...]],  # 2D: patches x dimensions
                "patch_count": number_of_patches
            }

            For multiple pages input:
            {
                "embeddings": [[[doc1_patches]], [[doc2_patches]], ...],  # 3D: documents x patches x dimensions
                "document_count": number_of_documents
            }
        """
        # Skip processing if endpoint not configured
        if not self.endpoint_url:
            logging.info(
                "ColQwen2 endpoint not configured - skipping embedding generation"
            )
            return None

        # Track if input was a single page
        is_single_page = not isinstance(document_pages, list)

        # Normalize input to list
        pages_list = (
            document_pages if isinstance(document_pages, list) else [document_pages]
        )

        try:
            # Use semaphore to limit concurrent requests to the endpoint
            page_numbers = [page.page_number for page in pages_list]
            logging.debug(
                f"Acquiring semaphore for embedding request (pages {page_numbers})"
            )
            async with self.semaphore:
                logging.debug(f"Semaphore acquired, processing pages {page_numbers}")

                # Prepare the request payload for multiple pages
                request_payload = self._prepare_payload(pages_list)

                logging.debug(
                    f"Sending request to ColQwen2 endpoint for pages {page_numbers}"
                )
                logging.debug(
                    f"Payload includes: {len(request_payload.images or [])} images, pooling_type: {request_payload.pooling_type}, pool_factor: {request_payload.pooling_config.get('pool_factor') if request_payload.pooling_config else None}"
                )

                # Get auth header
                headers = await self._get_auth_header()

                # Make async request to ColQwen2 endpoint
                if not self.endpoint_url:
                    logging.error("Endpoint URL is not configured")
                    return None

                timeout = aiohttp.ClientTimeout(total=self.request_timeout)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(
                        self.endpoint_url,
                        json=request_payload.model_dump(),
                        headers=headers,
                    ) as response:
                        if response.status == 200:
                            # Parse the JSON response
                            result = await response.json()

                            embeddings_dict = self._extract_embeddings(result)

                            if embeddings_dict:
                                # Get first embedding type for shape info
                                first_key = next(iter(embeddings_dict))
                                first_embeddings = embeddings_dict[first_key]
                                doc_count = len(first_embeddings)

                                if doc_count > 0 and len(first_embeddings[0]) > 0:
                                    patch_count = len(first_embeddings[0])
                                    dim = (
                                        len(first_embeddings[0][0])
                                        if len(first_embeddings[0][0]) > 0
                                        else 0
                                    )
                                    shape = f"{doc_count} docs x {patch_count} patches x {dim} dim"
                                    logging.debug(
                                        f"Successfully generated embeddings: {shape} for types: {list(embeddings_dict.keys())}"
                                    )

                                # Return format based on input type
                                if is_single_page:
                                    # Single page input - return single document embeddings (2D)
                                    single_page_embeddings = {}
                                    for emb_type, emb_data in embeddings_dict.items():
                                        single_page_embeddings[emb_type] = emb_data[
                                            0
                                        ]  # First (and only) document

                                    return {
                                        "embeddings": single_page_embeddings,
                                        "patch_count": len(first_embeddings[0]),
                                    }
                                else:
                                    # Multiple pages input - return full batch (3D)
                                    return {
                                        "embeddings": embeddings_dict,  # All embedding types
                                        "document_count": doc_count,
                                    }
                            else:
                                logging.error(
                                    "Failed to extract embeddings from response"
                                )
                                return None
                        else:
                            text = await response.text()
                            logging.error(
                                f"ColQwen2 endpoint returned status {response.status}: {text}"
                            )
                            return None

        except asyncio.TimeoutError:
            logging.error("Request to ColQwen2 endpoint timed out")
            return None
        except aiohttp.ClientError as e:
            logging.error(f"Request error: {str(e)}")
            return None
        except Exception as e:
            logging.error(f"Unexpected error generating embeddings: {str(e)}")
            return None

    def _prepare_payload(self, document_pages: List[DocumentPage]) -> EmbedRequest:
        """
        Prepare the request payload for ColQwen2 endpoint with multiple pages.

        Args:
            document_pages: List of DocumentPage models with images and text

        Returns:
            EmbedRequest object for the API request
        """
        images = []

        # Extract image data from all pages - ColQwen2 processes visual document content
        for document_page in document_pages:
            # Use the first image (main page image) if available
            page_image = document_page.images[0] if document_page.images else None

            # Add image if available
            if page_image:
                # Handle PIL Image, ImageFile, and bytes
                if isinstance(page_image, (Image.Image, ImageFile)):
                    image_base64 = self._image_to_base64(page_image)
                    images.append(image_base64)
                else:
                    # If it's bytes, convert to base64 directly
                    image_base64 = base64.b64encode(page_image).decode("utf-8")
                    images.append(image_base64)
            else:
                # If no image, we can't generate embeddings with ColQwen2
                logging.warning(
                    f"No page_image found for page {document_page.page_number}"
                )

        # Request both mean pooling and hierarchical pooling for multi-stage retrieval
        # Pool factor 3 provides optimal balance between compression and quality
        request = EmbedRequest(
            images=images,
            pooling_type=["mean_pooling", "hierarchical"],
            pooling_config={"pool_factor": 3},
        )

        # Validate payload structure
        if request.images:
            pool_factor = (
                request.pooling_config.get("pool_factor")
                if request.pooling_config
                else None
            )
            logging.debug(
                f"Prepared payload with {len(images)} images, hierarchical pooling enabled (factor: {pool_factor})"
            )

        return request

    def _image_to_base64(self, image: Union[Image.Image, ImageFile]) -> str:
        """
        Convert PIL Image to base64 string.

        Args:
            image: PIL Image or ImageFile object

        Returns:
            Base64 encoded image string
        """
        # Ensure image is in RGB format
        if image.mode != "RGB":
            image = image.convert("RGB")

        # Resize image if too large (ColQwen2 supports dynamic resolution up to 768 patches)
        if max(image.size) > self.max_image_size:
            ratio = self.max_image_size / max(image.size)
            new_width = int(image.size[0] * ratio)
            new_height = int(image.size[1] * ratio)
            new_size = (new_width, new_height)
            image = image.resize(new_size, Image.Resampling.LANCZOS)

        # Convert to base64
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=85)
        image_base64 = base64.b64encode(buffer.getvalue()).decode("utf-8")

        return image_base64

    def _extract_embeddings(
        self, response_data: Dict[str, Any]
    ) -> Optional[Dict[str, List[List[List[float]]]]]:
        """
        Extract embeddings from ColQwen2 endpoint response with multiple pooling types.

        Expected response format:
        {
            "hierarchical_pooled_embeddings": {
                "hierarchical": [[[...]], [[...]], ...]
            },
            "mean_row_pooled_embeddings": {
                "mean_pooling": [[[...]], [[...]], ...]
            },
            "mean_column_pooled_embeddings": {
                "mean_pooling": [[[...]], [[...]], ...]
            },
            "embeddings": [[[...]], [[...]], ...]  # Optional original embeddings
        }

        Args:
            response_data: Response JSON from the endpoint

        Returns:
            Dictionary with embedding types mapped to their vectors, or None if extraction failed
        """
        try:
            extracted_embeddings = {}

            # Extract hierarchical pooled embeddings (stored as "original" vector in QDRANT)
            if "hierarchical_pooled_embeddings" in response_data:
                hierarchical_data = response_data["hierarchical_pooled_embeddings"]
                if "hierarchical" in hierarchical_data:
                    extracted_embeddings["original"] = hierarchical_data["hierarchical"]
                    logging.debug(
                        "Extracted hierarchical embeddings for 'original' vector"
                    )

            # Extract mean row pooled embeddings
            if "mean_row_pooled_embeddings" in response_data:
                row_data = response_data["mean_row_pooled_embeddings"]
                if "mean_pooling" in row_data:
                    extracted_embeddings["mean_pooling_rows"] = row_data["mean_pooling"]
                    logging.debug("Extracted mean row pooled embeddings")

            # Extract mean column pooled embeddings
            if "mean_column_pooled_embeddings" in response_data:
                col_data = response_data["mean_column_pooled_embeddings"]
                if "mean_pooling" in col_data:
                    extracted_embeddings["mean_pooling_columns"] = col_data[
                        "mean_pooling"
                    ]
                    logging.debug("Extracted mean column pooled embeddings")

            # Fallback to original embeddings if available (for backward compatibility)
            if not extracted_embeddings and "embeddings" in response_data:
                extracted_embeddings["original"] = response_data["embeddings"]
                logging.debug("Using fallback original embeddings")

            if not extracted_embeddings:
                logging.error("No valid embeddings found in response")
                return None

            # Validate structure for at least one embedding type
            first_key = next(iter(extracted_embeddings))
            first_embeddings = extracted_embeddings[first_key]

            if not isinstance(first_embeddings, list) or len(first_embeddings) == 0:
                logging.error("Invalid embeddings structure")
                return None

            first_doc = first_embeddings[0]
            if not isinstance(first_doc, list) or len(first_doc) == 0:
                logging.error("Invalid document structure in embeddings")
                return None

            logging.debug(
                f"Successfully extracted {len(extracted_embeddings)} embedding types "
                f"with {len(first_embeddings)} documents each"
            )
            return extracted_embeddings

        except Exception as e:
            logging.error(f"Error extracting embeddings: {str(e)}")
            return None
