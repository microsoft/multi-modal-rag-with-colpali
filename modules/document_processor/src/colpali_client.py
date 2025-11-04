"""
ColQwen2 client for interfacing with Azure ML Online Endpoint.
Upgraded from ColPali to use the latest ColQwen2 model based on Qwen2-VL-2B-Instruct.
Uses Azure managed identity for authentication.
"""

import asyncio
import base64
import logging
import os
from io import BytesIO
from typing import Any, Dict, Optional

import aiohttp
from azure.identity.aio import DefaultAzureCredential, ManagedIdentityCredential
from PIL import Image
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


class ColPaliClient:
    """Client for communicating with ColQwen2 online endpoint using managed identity authentication."""

    def __init__(
        self,
        credential: Optional[DefaultAzureCredential | ManagedIdentityCredential] = None,
    ):
        self.endpoint_url = os.getenv("AML_EMBEDDING_ENDPOINT_URL")
        self.request_timeout = int(os.getenv("COLPALI_REQUEST_TIMEOUT", "120"))
        self.max_image_size = int(os.getenv("COLPALI_MAX_IMAGE_SIZE", "1536"))

        # Configure concurrency limit for embedding requests
        max_concurrent_requests = int(os.getenv("COLPALI_MAX_CONCURRENT_REQUESTS", "5"))
        self.semaphore = asyncio.Semaphore(max_concurrent_requests)

        if not self.endpoint_url:
            logging.warning(
                "AML_EMBEDDING_ENDPOINT_URL not set - ColQwen2 processing will be skipped"
            )
            self.credential = None
            return

        # Ensure endpoint URL has correct format
        if not self.endpoint_url.endswith("/score"):
            self.endpoint_url = f"{self.endpoint_url.rstrip('/')}/score"

        # Use provided credential or create new one
        try:
            self.credential = credential or DefaultAzureCredential()
            logging.info(
                f"ColQwen2 client initialized with managed identity for endpoint: {self.endpoint_url}"
            )
            logging.info(
                f"Concurrency limit: {max_concurrent_requests} concurrent requests"
            )
        except Exception as e:
            logging.error(f"Failed to initialize Azure managed identity: {str(e)}")
            self.credential = None

    async def _get_auth_header(self) -> Dict[str, str]:
        """Get authorization header with AAD token."""
        if not self.credential:
            return {}

        try:
            token = await self.credential.get_token("https://ml.azure.com/.default")
            return {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token.token}",
                "Accept": "application/json",
            }
        except Exception as e:
            logging.error(f"Failed to get AAD token: {str(e)}")
            return {}

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)),
        reraise=True,
    )
    async def generate_embeddings(
        self, document_chunk: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Generate hierarchically pooled embeddings for a document chunk using ColQwen2 online endpoint.

        ColQwen2 returns both original patch-level embeddings and hierarchically pooled embeddings.
        The hierarchical pooling compresses embeddings using a configurable pool factor.

        Args:
            document_chunk: Document chunk containing images (ColQwen2 is image-focused)

        Returns:
            Dictionary with 'original_embeddings' and 'pooled_embeddings' keys, or None if generation failed.
            Structure: {
                "original_embeddings": [[patch1_dim1, ...], [patch2_dim1, ...]],
                "pooled_embeddings": [[pooled_patch1, ...], ...],
                "pool_factor": 4,
                "compression_ratio": 0.25
            }
        """
        # Skip processing if endpoint not configured
        if not self.credential:
            logging.info(
                "ColQwen2 endpoint not configured - skipping embedding generation"
            )
            return None

        try:
            # Use semaphore to limit concurrent requests to the endpoint
            logging.debug(
                f"Acquiring semaphore for embedding request (page {document_chunk.get('page_number', 'unknown')})"
            )
            async with self.semaphore:
                logging.debug(
                    f"Semaphore acquired, processing page {document_chunk.get('page_number', 'unknown')}"
                )

                # Prepare the request payload
                payload = self._prepare_payload(document_chunk)

                logging.debug(
                    f"Sending request to ColQwen2 endpoint for chunk from {document_chunk.get('source_file', 'unknown')}"
                )
                logging.debug(
                    f"Payload includes: {len(payload.get('images', []))} images, pooling_type: {payload.get('pooling_type')}, pool_factor: {payload.get('pooling_config', {}).get('pool_factor')}"
                )

                # Get auth header with fresh token
                headers = await self._get_auth_header()
                if not headers:
                    logging.error("Failed to get authorization header")
                    return None

                # Make async request to ColQwen2 endpoint
                if not self.endpoint_url:
                    logging.error("Endpoint URL is not configured")
                    return None

                timeout = aiohttp.ClientTimeout(total=self.request_timeout)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(
                        self.endpoint_url, json=payload, headers=headers
                    ) as response:
                        if response.status == 200:
                            # Parse the JSON response
                            result = await response.json()

                            embeddings = self._extract_embeddings(result)

                            if embeddings:
                                if (
                                    "original_embeddings" in embeddings
                                    and embeddings["original_embeddings"]
                                ):
                                    orig_shape = f"{len(embeddings['original_embeddings'])}x{len(embeddings['original_embeddings'][0]) if embeddings['original_embeddings'] else 0}"
                                    logging.debug(
                                        f"Successfully generated original embeddings shape: {orig_shape}"
                                    )
                                if (
                                    "pooled_embeddings" in embeddings
                                    and embeddings["pooled_embeddings"]
                                ):
                                    pooled_shape = f"{len(embeddings['pooled_embeddings'])}x{len(embeddings['pooled_embeddings'][0]) if embeddings['pooled_embeddings'] else 0}"
                                    logging.debug(
                                        f"Successfully generated pooled embeddings shape: {pooled_shape}"
                                    )
                            return embeddings
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

    def _prepare_payload(self, document_chunk: Dict[str, Any]) -> Dict[str, Any]:
        """
        Prepare the request payload for ColQwen2 endpoint with hierarchical pooling.

        Args:
            document_chunk: Document chunk data

        Returns:
            Formatted payload for the API request
        """
        # 3 is recommended by ColPali team: https://github.com/illuin-tech/colpali?tab=readme-ov-file#token-pooling
        payload = {
            "images": [],
            "pooling_type": "hierarchical",  # Request hierarchical pooling from ColQwen2
            "pooling_config": {"pool_factor": 3},
        }

        # Extract image data - ColQwen2 is primarily image-focused
        page_image = document_chunk.get("page_image")

        # Add image if available
        if page_image:
            # Convert PIL Image to base64 string
            image_base64 = self._image_to_base64(page_image)
            payload["images"].append(image_base64)
        else:
            # If no image, we can't generate embeddings with ColQwen2
            logging.warning(
                f"No page_image found for chunk from {document_chunk.get('source_file', 'unknown')}"
            )
            payload["images"] = []

        # Validate payload structure
        if payload["images"]:
            logging.debug(
                f"Prepared payload with hierarchical pooling enabled (factor: {payload['pooling_config']['pool_factor']})"
            )

        return payload

    def _image_to_base64(self, image: Image.Image) -> str:
        """
        Convert PIL Image to base64 string.

        Args:
            image: PIL Image object

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
    ) -> Optional[Dict[str, Any]]:
        """
        Extract embeddings from ColQwen2 endpoint response with hierarchical pooling support.

        New ColQwen2 API returns structured response with original and pooled embeddings:
        {
            "embeddings": {
                "original_embeddings": [...],  # Full patch-level embeddings
                "pooled_embeddings": [...],    # Hierarchically pooled embeddings
                "pool_factor": 4,
                "compression_ratio": 0.25
            },
            "status": "success"
        }

        Args:
            response_data: Response JSON from the endpoint

        Returns:
            Dictionary containing original_embeddings and pooled_embeddings, or None if extraction failed
        """
        try:
            # Check for error status first
            if response_data.get("status") == "error":
                logging.error(
                    f"ColQwen2 endpoint returned error: {response_data.get('error', 'Unknown error')}"
                )
                return None

            # Extract structured embeddings response
            if (
                "embeddings" in response_data
                and response_data.get("status") == "success"
            ):
                embeddings_data = response_data["embeddings"]

                # Validate structure: should be dict with original_embeddings and optionally pooled_embeddings
                if isinstance(embeddings_data, dict):
                    result = {}

                    # Extract original embeddings
                    if "original_embeddings" in embeddings_data:
                        original = embeddings_data["original_embeddings"]
                        if isinstance(original, list) and len(original) > 0:
                            result["original_embeddings"] = original
                            logging.debug(
                                f"Extracted {len(original)} original patch embeddings"
                            )

                    # Extract pooled embeddings (if hierarchical pooling was applied)
                    if (
                        "pooled_embeddings" in embeddings_data
                        and embeddings_data["pooled_embeddings"]
                    ):
                        pooled = embeddings_data["pooled_embeddings"]
                        if isinstance(pooled, list) and len(pooled) > 0:
                            result["pooled_embeddings"] = pooled
                            result["pool_factor"] = embeddings_data.get(
                                "pool_factor", 1
                            )
                            result["compression_ratio"] = embeddings_data.get(
                                "compression_ratio", 1.0
                            )
                            logging.debug(
                                f"Extracted {len(pooled)} pooled embeddings with factor {result['pool_factor']}"
                            )

                    if result:
                        return result

            logging.warning("Could not find valid embeddings in response data")
            logging.debug(
                f"Response keys: {list(response_data.keys()) if isinstance(response_data, dict) else 'Not a dict'}"
            )
            return None

        except Exception as e:
            logging.error(f"Error extracting embeddings from response: {str(e)}")
            return None
