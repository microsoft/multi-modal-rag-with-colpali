# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
# ColPali related code taken from: https://github.com/microsoft/dstoolkit-multi-modal-rag-with-colpali
"""
ColVision Model Inference Module (vLLM-backed).

Targets ColQwen3 (TomoroAI/tomoro-colqwen3-embed-4b) via vLLM as a GPU
sidecar.

The shim is CPU-only — it owns the HF processor for tokenization +
patch-grid computation, proxies image/text batches to vLLM over loopback
HTTP using tmpfs-shared file paths for images, and runs hierarchical /
mean-row / mean-column pooling as pure post-processing on the per-token
embeddings vLLM returns.
"""

import asyncio
import base64
import logging
import os
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple, cast

import torch
from colpali_engine.compression.token_pooling import HierarchicalTokenPooler
from PIL import Image
from transformers import AutoProcessor

from .models import EmbedRequest, EmbedResponse, PoolingType
from .setup_logging import trace_operation
from .vllm_client import VLLMEmbeddingClient, get_client

logger = logging.getLogger(__name__)


class ColPaliInference:
    """
    ColVision model inference handler for embedding generation.

    Loads only the HF processor (CPU) for tokenization + patch grid
    computation, and the colpali_engine HierarchicalTokenPooler. Forward
    passes are delegated to the vLLM sidecar via VLLMEmbeddingClient.
    """

    def __init__(
        self,
        model_id: Optional[str] = None,
        vllm_client: Optional[VLLMEmbeddingClient] = None,
    ):
        self.processor: Optional[Any] = None
        self.hierarchical_pooler: Optional[HierarchicalTokenPooler] = None
        self.is_initialized = False

        self.model_id = model_id or os.getenv(
            "MODEL_ID", "TomoroAI/tomoro-colqwen3-embed-4b"
        )
        self.vllm_client = vllm_client or get_client()

        logger.info("ColPaliInference configured for model: %s", self.model_id)

    def _get_cached_model_path(self) -> str:
        """Resolve the local processor path on the shared PVC."""
        model_directory_path = os.getenv("MODEL_DIRECTORY_PATH")
        if not model_directory_path:
            raise RuntimeError(
                "MODEL_DIRECTORY_PATH is required — the shim loads the processor "
                "from the shared PVC populated by the init container."
            )
        model_name = self.model_id.split("/")[-1]
        model_path = os.path.join(model_directory_path, model_name)
        if not os.path.isdir(model_path):
            raise FileNotFoundError(
                f"Processor not found at {model_path}. "
                "Ensure the init container has downloaded the model snapshot."
            )
        logger.info("Using PVC processor path: %s", model_path)
        return model_path

    def initialize(self) -> None:
        """Initialize the processor and pooler. Does not block on vLLM readiness."""
        if self.is_initialized:
            logger.debug("Inference handler already initialized")
            return

        logger.info("Initializing ColQwen3 processor (CPU-only)")
        cached_path = self._get_cached_model_path()

        logger.info("Loading processor using AutoProcessor")
        self.processor = AutoProcessor.from_pretrained(
            cached_path,
            local_files_only=True,
            trust_remote_code=True,
        )

        self.hierarchical_pooler = HierarchicalTokenPooler()
        self.is_initialized = True
        logger.info("ColQwen3 processor initialization complete")

    def base64_to_image(self, base64_string: str) -> Image.Image:
        """Convert a base64 string (optionally data-URI-prefixed) to a PIL Image."""
        try:
            if base64_string.startswith("data:image"):
                base64_string = base64_string.split(",", 1)[1]
            image_data = base64.b64decode(base64_string)
            image = Image.open(BytesIO(image_data))
            if image.mode != "RGB":
                image = image.convert("RGB")
            return image
        except Exception as e:
            logger.error("Failed to decode base64 image: %s", e)
            raise ValueError(f"Invalid base64 image data: {e}")

    def _apply_hierarchical_pooling(
        self,
        embeddings: torch.Tensor,
        pooling_config: Optional[Dict[str, Any]] = None,
    ) -> torch.Tensor:
        """Apply hierarchical token pooling via colpali_engine's helper."""
        if self.hierarchical_pooler is None:
            raise RuntimeError("Hierarchical pooler not initialized")

        effective_config = pooling_config or {"pool_factor": 3}
        pool_factor = effective_config.get("pool_factor", 3)

        pooled_result = self.hierarchical_pooler.pool_embeddings(
            embeddings,
            pool_factor=pool_factor,
            padding=True,
            padding_side="right",
        )

        if torch.is_tensor(pooled_result):
            return pooled_result
        if hasattr(pooled_result, "embeddings") and torch.is_tensor(
            cast(Any, pooled_result).embeddings
        ):
            return cast(Any, pooled_result).embeddings
        if isinstance(pooled_result, (list, tuple)) and pooled_result:
            return torch.stack(list(pooled_result))
        raise TypeError(f"Unexpected type from pool_embeddings: {type(pooled_result)}")

    def _get_patches(self, image_size: Tuple[int, int]) -> Tuple[int, int]:
        """Get (x_patches, y_patches) for one image via the HF processor (CPU)."""
        if self.processor is None:
            raise RuntimeError("Processor must be initialized before computing patches")

        merge_size = self.processor.image_processor.merge_size
        return self.processor.get_n_patches(
            image_size,
            spatial_merge_size=merge_size,
        )

    def _get_image_token_id(self) -> int:
        """Get the image token ID used to identify image tokens in input_ids."""
        if self.processor is None:
            raise RuntimeError("Processor must be initialized")
        return self.processor.image_token_id

    @trace_operation("generate_embeddings")
    async def generate_embeddings(self, request: EmbedRequest) -> EmbedResponse:
        """Main async inference entrypoint called by the FastAPI handler."""
        if not self.is_initialized or self.processor is None:
            raise RuntimeError(
                "Inference handler not initialized. Call initialize() first."
            )

        logger.debug("Processing inference request")

        if request.images:
            return await self._process_images_from_request(request)
        if request.texts:
            return await self._process_texts_from_request(request)
        raise ValueError("Request must contain either 'images' or 'texts' field")

    async def _process_images_from_request(
        self, request: EmbedRequest
    ) -> EmbedResponse:
        """Image embedding path: shim tokenizes + vLLM embeds + shim pools."""
        if request.images is None:
            raise ValueError("Request must contain 'images' field with image content")

        # Decode base64 → PIL.
        images: List[Image.Image] = []
        for i, image_data in enumerate(request.images):
            try:
                images.append(self.base64_to_image(image_data))
            except Exception as e:
                logger.error("Failed to process image at index %s: %s", i, e)
                raise ValueError(f"Failed to process image at index {i}: {e}")

        logger.debug("Processing %d images", len(images))

        pooling_types = set(request.pooling_type)
        needs_input_ids = PoolingType.MEAN_POOLING in pooling_types

        # For mean pooling we need the exact token-id sequence vLLM ran
        # against. Querying vLLM's /tokenize endpoint with the same chat
        # payload that embed_images sends is the only reliable way to get
        # this — the shim's local HF processor uses colpali_engine's
        # image-only template, which produces a different prompt structure
        # than vLLM's chat-template path and therefore disagrees on token
        # counts (especially for ColQwen3 dynamic-resolution images).
        per_image_input_ids: Optional[List[torch.Tensor]] = None
        if needs_input_ids:
            tokenize_results = await asyncio.gather(
                *(self.vllm_client.tokenize_chat_image(image) for image in images)
            )
            per_image_input_ids = [
                torch.tensor(ids, dtype=torch.long) for ids in tokenize_results
            ]

        # Call vLLM for embeddings (one variable-length tensor per image).
        embeddings_per_image = await self.vllm_client.embed_images(images)

        if len(embeddings_per_image) != len(images):
            raise RuntimeError(
                f"vLLM returned {len(embeddings_per_image)} embeddings for "
                f"{len(images)} images"
            )

        response: Dict[str, Any] = {}

        for pooling_type in pooling_types:
            if pooling_type == PoolingType.MEAN_POOLING:
                logger.debug("Applying mean pooling...")
                assert per_image_input_ids is not None
                image_token_id = self._get_image_token_id()

                pooled_rows_batch: List[List[List[float]]] = []
                pooled_cols_batch: List[List[List[float]]] = []

                for image_embedding, tokenized_image, image in zip(
                    embeddings_per_image, per_image_input_ids, images
                ):
                    x_patches, y_patches = self._get_patches(image.size)

                    image_tokens_mask = tokenized_image == image_token_id

                    expected = x_patches * y_patches
                    actual = int(image_tokens_mask.sum().item())
                    if actual != expected:
                        raise RuntimeError(
                            f"Image-token count mismatch: tokenizer reported "
                            f"{actual} image tokens but patch grid expects "
                            f"{x_patches}x{y_patches}={expected}."
                        )
                    if image_embedding.shape[0] != tokenized_image.shape[0]:
                        raise RuntimeError(
                            f"Embedding/input_ids length mismatch: "
                            f"{image_embedding.shape[0]} vs "
                            f"{tokenized_image.shape[0]}. vLLM and shim "
                            "tokenization are out of sync."
                        )

                    embedding_dim = image_embedding.shape[-1]
                    image_tokens = image_embedding[image_tokens_mask].view(
                        x_patches, y_patches, embedding_dim
                    )
                    pooled_by_rows = torch.mean(image_tokens, dim=0)
                    pooled_by_columns = torch.mean(image_tokens, dim=1)

                    image_token_idxs = torch.nonzero(
                        image_tokens_mask.int(), as_tuple=False
                    )
                    first_image_token_idx = int(image_token_idxs[0].item())
                    last_image_token_idx = int(image_token_idxs[-1].item())

                    prefix_tokens = image_embedding[:first_image_token_idx]
                    postfix_tokens = image_embedding[last_image_token_idx + 1 :]

                    pooled_by_rows_full = torch.cat(
                        (prefix_tokens, pooled_by_rows, postfix_tokens), dim=0
                    )
                    pooled_by_columns_full = torch.cat(
                        (prefix_tokens, pooled_by_columns, postfix_tokens), dim=0
                    )

                    pooled_rows_batch.append(
                        pooled_by_rows_full.float().numpy().tolist()
                    )
                    pooled_cols_batch.append(
                        pooled_by_columns_full.float().numpy().tolist()
                    )

                response["mean_row_pooled_embeddings"] = pooled_rows_batch
                response["mean_column_pooled_embeddings"] = pooled_cols_batch
                logger.debug("Mean pooling completed")

            elif pooling_type == PoolingType.HIERARCHICAL_POOLING:
                logger.debug("Applying hierarchical pooling...")
                # Apply per-image (sequence lengths vary).
                pooled_per_image = [
                    self._apply_hierarchical_pooling(
                        emb.unsqueeze(0), request.pooling_config
                    )
                    for emb in embeddings_per_image
                ]
                response["hierarchical_pooled_embeddings"] = [
                    pooled.squeeze(0).float().numpy().tolist()
                    for pooled in pooled_per_image
                ]
                logger.debug("Hierarchical pooling completed")

            elif pooling_type == PoolingType.NONE:
                logger.debug("Adding original (un-pooled) embeddings...")
                response["embeddings"] = [
                    emb.float().numpy().tolist() for emb in embeddings_per_image
                ]
                logger.debug("Original embeddings added")

        logger.info("Generated embeddings for %s images", len(request.images))
        return EmbedResponse(**response)

    async def _process_texts_from_request(self, request: EmbedRequest) -> EmbedResponse:
        """Text embedding path: vLLM handles tokenization internally."""
        if request.texts is None:
            raise ValueError("Request must contain 'texts' field with text content")

        logger.debug("Processing %d text queries", len(request.texts))
        embeddings_per_text = await self.vllm_client.embed_texts(list(request.texts))

        final_embeddings = [emb.float().numpy().tolist() for emb in embeddings_per_text]
        logger.info("Generated embeddings for %s text queries", len(request.texts))
        return EmbedResponse(embeddings=final_embeddings)


# Global instance (one per uvicorn worker process)
_inference_instance: Optional[ColPaliInference] = None


def get_inference_instance() -> ColPaliInference:
    """Get or create the per-process inference instance."""
    global _inference_instance
    if _inference_instance is None:
        _inference_instance = ColPaliInference()
    return _inference_instance
