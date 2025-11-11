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

from .models import DocumentPage, EmbedHealthResponse, EmbedRequest, PoolingType


class ColPaliClient:
    """Client for communicating with ColQwen2 service in Kubernetes."""

    def __init__(
        self,
        require_endpoint: bool = True,
    ):
        # Configure for Kubernetes ColQwen2 service
        self.endpoint_url = os.getenv("COLPALI_ENDPOINT")
        self.request_timeout = int(os.getenv("COLPALI_REQUEST_TIMEOUT", "120"))
        self.max_image_size = int(os.getenv("COLPALI_MAX_IMAGE_SIZE", "1536"))

        # Configure concurrency limit for embedding requests
        max_concurrent_requests = int(os.getenv("COLPALI_MAX_CONCURRENT_REQUESTS", "5"))
        self.semaphore = asyncio.Semaphore(max_concurrent_requests)

        if require_endpoint and not self.endpoint_url:
            raise ValueError(
                "COLPALI_ENDPOINT environment variable is required but not set"
            )

        # Ensure endpoint URL has correct format for embedding endpoint
        if self.endpoint_url and not self.endpoint_url.endswith("/embeddings"):
            self.endpoint_url = f"{self.endpoint_url.rstrip('/')}/embeddings"

        logging.info(
            "ColQwen2 client initialized for Kubernetes endpoint: %s", self.endpoint_url
        )
        logging.info(
            "Concurrency limit: %s concurrent requests", max_concurrent_requests
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
                            "Health check failed with status %s", response.status
                        )
                        return None
        except Exception as e:
            logging.error("Health check error: %s", str(e))
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
            Dictionary with embeddings as returned by the API, or None if generation failed.

            The response contains the following keys:
            - embeddings: Raw embeddings from the model
            - hierarchical_pooled_embeddings: Hierarchically pooled embeddings
            - mean_row_pooled_embeddings: Row-wise mean pooled embeddings
            - mean_column_pooled_embeddings: Column-wise mean pooled embeddings

            For single page input: Each key contains the embeddings for that single page
            For multiple pages input: Each key contains a list of embeddings, one per page
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
                "Acquiring semaphore for embedding request (pages %s)", page_numbers
            )
            async with self.semaphore:
                logging.debug("Semaphore acquired, processing pages %s", page_numbers)

                # Prepare the request payload for multiple pages
                request_payload = self._prepare_payload(pages_list)

                logging.debug(
                    "Sending request to ColQwen2 endpoint for pages %s", page_numbers
                )
                logging.debug(
                    "Payload includes: %s images, pooling_type: %s, pool_factor: %s",
                    len(request_payload.images or []),
                    request_payload.pooling_type,
                    request_payload.pooling_config.get("pool_factor")
                    if request_payload.pooling_config
                    else None,
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

                            if result:
                                # Return format based on input type
                                if is_single_page:
                                    # Single page input - extract first document from each embedding type
                                    single_page_result = {}
                                    for key in [
                                        "embeddings",
                                        "hierarchical_pooled_embeddings",
                                        "mean_row_pooled_embeddings",
                                        "mean_column_pooled_embeddings",
                                    ]:
                                        if key in result:
                                            # Get first document from the batch
                                            single_page_result[key] = (
                                                result[key][0]
                                                if isinstance(result[key], list)
                                                and len(result[key]) > 0
                                                else result[key]
                                            )

                                    return single_page_result
                                else:
                                    # Multiple pages - return as-is
                                    return result
                            else:
                                logging.error("Empty response from endpoint")
                                return None
                        else:
                            text = await response.text()
                            logging.error(
                                "ColQwen2 endpoint returned status %s: %s",
                                response.status,
                                text,
                            )
                            return None

        except asyncio.TimeoutError:
            logging.error("Request to ColQwen2 endpoint timed out")
            return None
        except aiohttp.ClientError as e:
            logging.error("Request error: %s", str(e))
            return None
        except Exception as e:
            logging.error("Unexpected error generating embeddings: %s", str(e))
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
            # Use the single page image
            page_image = document_page.image_content

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
                    "No image_content found for page %s", document_page.page_number
                )

        # Request both mean pooling and hierarchical pooling for multi-stage retrieval
        # Pool factor 3 provides optimal balance between compression and quality
        request = EmbedRequest(
            images=images,
            pooling_type=[PoolingType.MEAN_POOLING, PoolingType.HIERARCHICAL_POOLING],
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
                "Prepared payload with %s images, hierarchical pooling enabled (factor: %s)",
                len(images),
                pool_factor,
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
