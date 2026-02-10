# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""
ColVision Model Inference Module

Supports multiple ColBERT-style vision models via HuggingFace transformers:
- ColPali (vidore/colpali-v1.3-hf)
- ColQwen2 (vidore/colqwen2-v1.0-hf)
- ColQwen3 (TomoroAI/tomoro-colqwen3-embed-4b)

Handles model initialization and inference logic for Kubernetes deployment.
"""

import base64
import logging
import os
import time
from enum import Enum
from io import BytesIO
from typing import Any, Dict, List, Optional, cast

import torch
from colpali_engine.compression.token_pooling import HierarchicalTokenPooler
from huggingface_hub import snapshot_download
from PIL import Image
from transformers import (
    AutoModel,
    AutoProcessor,
    ColPaliForRetrieval,
    ColPaliProcessor,
    ColQwen2ForRetrieval,
    ColQwen2Processor,
)
from transformers.utils.import_utils import is_flash_attn_2_available

from .models import EmbedRequest, EmbedResponse, PoolingType

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    force=True,
)
logger = logging.getLogger(__name__)


class ModelType(str, Enum):
    """Supported ColVision model types."""

    COLPALI = "colpali"
    COLQWEN2 = "colqwen2"
    COLQWEN3 = "colqwen3"


def detect_model_type(model_id: str) -> ModelType:
    """Detect model type from model ID."""
    model_id_lower = model_id.lower()
    if "colqwen3" in model_id_lower or "qwen3" in model_id_lower:
        return ModelType.COLQWEN3
    elif "colpali" in model_id_lower or "paligemma" in model_id_lower:
        return ModelType.COLPALI
    else:
        return ModelType.COLQWEN2  # Default


class ColPaliInference:
    """
    ColVision model inference handler for embedding generation.
    Supports ColPali, ColQwen2, and ColQwen3 models via HuggingFace transformers.
    Manages model lifecycle and provides inference capabilities for both images and text.
    """

    def __init__(self, model_id: Optional[str] = None):
        """
        Initialize the inference handler.

        Args:
            model_id: HuggingFace model ID. If not provided, uses MODEL_ID env var
                     or defaults to ColQwen2.
        """
        self.model: Optional[Any] = None
        self.processor: Optional[Any] = None
        self.device: Optional[torch.device] = None
        self.hierarchical_pooler: Optional[HierarchicalTokenPooler] = None
        self.is_initialized = False

        # Get model ID from parameter, env var, or default
        self.model_id = model_id or os.getenv("MODEL_ID", "vidore/colqwen2-v1.0-hf")
        self.model_type = detect_model_type(self.model_id)

        self.device_map = "auto"
        self.torch_dtype = None

        logger.info(f"ColPaliInference configured for model: {self.model_id}")
        logger.info(f"Detected model type: {self.model_type}")

    def _get_model_class(self):
        """Get the correct model class based on model type.

        Returns:
            The appropriate model class for loading from HuggingFace
        """
        if self.model_type == ModelType.COLPALI:
            return ColPaliForRetrieval

        if self.model_type == ModelType.COLQWEN2:
            return ColQwen2ForRetrieval

        if self.model_type == ModelType.COLQWEN3:
            # ColQwen3 uses trust_remote_code with custom model code
            return AutoModel

        raise ValueError(f"Unknown model type: {self.model_type}")

    def _get_processor_class(self):
        """Get the correct processor class based on model type.

        Returns:
            The appropriate processor class for loading from HuggingFace
        """
        if self.model_type == ModelType.COLPALI:
            return ColPaliProcessor

        if self.model_type == ModelType.COLQWEN2:
            return ColQwen2Processor

        if self.model_type == ModelType.COLQWEN3:
            # ColQwen3 uses trust_remote_code with custom processor code
            return AutoProcessor

        raise ValueError(f"Unknown model type: {self.model_type}")

    def _download_model_if_needed(self):
        """Pre-download model to HuggingFace cache for offline use using snapshot_download."""

        start_time = time.time()
        hf_home = os.getenv("HF_HOME", "~/.cache/huggingface")

        logger.info("Starting model download to HuggingFace cache")
        logger.info("Model ID: %s", self.model_id)
        logger.info("Model Type: %s", self.model_type)
        logger.info("HF_HOME: %s", hf_home)

        try:
            # Use snapshot_download to ensure ALL files are cached for offline use
            # This is the recommended approach per HuggingFace docs
            logger.info(
                "Downloading complete model repository using snapshot_download..."
            )
            snapshot_download(
                repo_id=self.model_id,
                repo_type="model",
                local_dir=None,  # Use default HF cache
            )
            logger.info("Model repository download complete")

        except Exception as error:
            logger.error("Download failed: %s", error)
            raise

        total_time = time.time() - start_time
        logger.info("Download complete in %.2f minutes", total_time / 60)

    def _get_processor_kwargs(self, for_download: bool = False) -> Dict[str, Any]:
        """Get processor loading kwargs based on model type."""
        kwargs: Dict[str, Any] = {}

        if self.model_type == "colqwen3":
            kwargs["trust_remote_code"] = True
            kwargs["max_num_visual_tokens"] = 1280

        if not for_download:
            kwargs["local_files_only"] = True

        return kwargs

    def _get_model_kwargs(self, for_download: bool = False) -> Dict[str, Any]:
        """Get model loading kwargs based on model type."""
        kwargs: Dict[str, Any] = {
            "torch_dtype": torch.float32 if for_download else self.torch_dtype,
            "device_map": "cpu"
            if for_download
            else ("auto" if self.device and self.device.type == "cuda" else None),
        }

        if for_download:
            kwargs["low_cpu_mem_usage"] = True
        else:
            kwargs["local_files_only"] = True

        # Model-specific kwargs
        if self.model_type == "colqwen3":
            kwargs["trust_remote_code"] = True
            # ColQwen3 uses 'dtype' parameter instead of 'torch_dtype'
            # and requires bf16 for flash attention
            if not for_download:
                del kwargs["torch_dtype"]
                kwargs["dtype"] = torch.bfloat16

        # Add attention implementation if available
        if not for_download and is_flash_attn_2_available():
            # Only use flash_attention_2 for models that support it well
            if self.model_type in ("colqwen2", "colqwen3"):
                kwargs["attn_implementation"] = "flash_attention_2"
        elif not for_download:
            # Use SDPA as fallback for ColQwen2/ColQwen3
            if self.model_type in ("colqwen2", "colqwen3"):
                kwargs["attn_implementation"] = "sdpa"

        return kwargs

    def _get_cached_model_path(self) -> str:
        """Get the local cached model path for offline loading.

        When HF_HUB_OFFLINE=1, we must pass the actual local path to from_pretrained,
        not the model ID, because passing model ID still triggers an API call.
        """
        hf_home = os.getenv("HF_HOME", "~/.cache/huggingface")

        # Build the expected cache directory path
        # Format: HF_HOME/hub/models--{org}--{model}/snapshots/{hash}/
        model_id_path = self.model_id.replace("/", "--")
        hub_path = os.path.join(hf_home, "hub", f"models--{model_id_path}")

        # Find the snapshot directory
        snapshots_path = os.path.join(hub_path, "snapshots")
        if os.path.exists(snapshots_path):
            # Get the snapshot
            snapshots = os.listdir(snapshots_path)
            if snapshots:
                model_path = os.path.join(snapshots_path, snapshots[0])
                logger.info(f"Using cached model path: {model_path}")
                return model_path

        raise FileNotFoundError(
            f"Model not found in cache at {hub_path}. "
            f"Expected snapshots at {snapshots_path}. "
            f"Ensure the init container has downloaded the model before running in offline mode."
        )

    def initialize(self):
        """Initialize the model and processor."""
        if self.is_initialized:
            logger.debug("Model already initialized")
            return

        logger.info(f"Initializing {self.model_type} model for inference")
        logger.info(f"Model ID: {self.model_id}")

        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.torch_dtype = (
            torch.bfloat16 if torch.cuda.is_available() else torch.float32
        )
        hf_home = os.getenv("HF_HOME", "~/.cache/huggingface")
        logger.info("Device: %s", self.device)
        logger.info("HF_HOME: %s", hf_home)

        try:
            processor_class = self._get_processor_class()
            model_class = self._get_model_class()

            # Use cached path for offline mode
            cached_path = self._get_cached_model_path()

            logger.info("Loading processor using %s...", processor_class.__name__)
            processor_kwargs = self._get_processor_kwargs()
            self.processor = processor_class.from_pretrained(
                cached_path, **processor_kwargs
            )

            logger.info("Loading model using %s...", model_class.__name__)
            model_kwargs = self._get_model_kwargs()
            self.model = model_class.from_pretrained(cached_path, **model_kwargs)

            # Move to device if not using device_map
            if model_kwargs.get("device_map") is None and self.device is not None:
                self.model.to(self.device)

            self.model.eval()
            self.hierarchical_pooler = HierarchicalTokenPooler()

            logger.info(
                f"{self.model_type} model initialization completed successfully"
            )
            self.is_initialized = True

        except Exception as e:
            logger.error("Failed to load model: %s", str(e))
            logger.error("HF_HOME: %s", hf_home)
            raise RuntimeError(
                f"Failed to load {self.model_type} model. "
                f"Ensure HF_HOME is set and init container downloaded the model. Error: {e}"
            )

    def base64_to_image(self, base64_string: str) -> Image.Image:
        """
        Convert base64 string to PIL Image.

        Args:
            base64_string (str): Base64 encoded image

        Returns:
            PIL.Image: Decoded image
        """
        try:
            # Remove data URL prefix if present
            if base64_string.startswith("data:image"):
                base64_string = base64_string.split(",")[1]

            # Decode base64
            image_data = base64.b64decode(base64_string)
            image = Image.open(BytesIO(image_data))

            # Convert to RGB if necessary
            if image.mode != "RGB":
                image = image.convert("RGB")

            return image

        except Exception as e:
            logger.error("Failed to decode base64 image: %s", str(e))
            raise ValueError("Invalid base64 image data: %s" % str(e))

    def _apply_hierarchical_pooling(
        self, embeddings: torch.Tensor, pooling_config: Optional[Dict[str, Any]] = None
    ) -> torch.Tensor:
        """Apply hierarchical token pooling using ColPali's helper."""
        try:
            if self.hierarchical_pooler is None:
                raise RuntimeError("Hierarchical pooler not initialized")

            logger.debug("Applying hierarchical token pooling...")

            original_shape = embeddings.shape
            logger.debug("Original embeddings shape: %s", original_shape)

            effective_config = pooling_config or {"pool_factor": 3}
            pool_factor = effective_config.get("pool_factor", 3)

            pooled_result = self.hierarchical_pooler.pool_embeddings(
                embeddings,
                pool_factor=pool_factor,
                padding=True,
                padding_side="right",
            )

            if torch.is_tensor(pooled_result):
                pooled_tensor = pooled_result
            elif hasattr(pooled_result, "embeddings") and torch.is_tensor(
                cast(Any, pooled_result).embeddings
            ):
                pooled_tensor = cast(Any, pooled_result).embeddings
            elif isinstance(pooled_result, (list, tuple)) and pooled_result:
                pooled_tensor = torch.stack(list(pooled_result))
            else:
                raise TypeError(
                    f"Unexpected type from pool_embeddings: {type(pooled_result)}"
                )

            pooled_shape = pooled_tensor.shape
            logger.debug("Pooled embeddings shape: %s", pooled_shape)

            if len(original_shape) >= 3 and len(pooled_shape) >= 3:
                original_size = original_shape[1] * original_shape[2]
                pooled_size = pooled_shape[1] * pooled_shape[2]
                if pooled_size > 0:
                    compression_ratio = original_size / pooled_size
                    logger.debug(
                        "Pooling completed - compression ratio: %.2fx (from %d to %d elements)",
                        compression_ratio,
                        original_size,
                        pooled_size,
                    )

            return pooled_tensor

        except Exception as e:
            logger.error("Hierarchical pooling failed: %s", str(e))
            raise

    def _get_patches(
        self,
        image_size,
        batch_images: Optional[Dict[str, Any]] = None,
        image_index: int = 0,
    ):
        """Get the number of patches for x and y dimensions.

        Used for mean pooling to reshape image tokens into a 2D grid.

        Args:
            image_size: Tuple of (width, height) in pixels (used for ColPali/ColQwen3)
            batch_images: Optional processed batch containing image_grid_thw (used for ColQwen2)
            image_index: Index of the image in the batch (used for ColQwen2)

        Returns:
            Tuple of (x_patches, y_patches)
        """
        if self.processor is None or self.model is None:
            raise RuntimeError(
                "Model and processor must be initialized before computing patches"
            )

        if self.model_type == ModelType.COLPALI:
            # ColPali: uses patch_size from model
            return self.processor.get_n_patches(
                image_size,
                patch_size=self.model.patch_size,
            )

        if self.model_type == ModelType.COLQWEN2:
            # ColQwen2: Use image_grid_thw from processed batch which contains grid dimensions
            # IMPORTANT: image_grid_thw is BEFORE spatial merge, must divide by merge_size
            if batch_images is not None:
                image_grid_thw = batch_images.get("image_grid_thw")
                if image_grid_thw is not None:
                    # image_grid_thw shape: (batch, 3) where each row is (temporal, height, width)
                    t, h, w = image_grid_thw[image_index].tolist()
                    # Divide by merge_size to get actual token grid dimensions
                    merge_size = self.processor.image_processor.merge_size
                    x_patches = int(w) // merge_size
                    y_patches = int(h) // merge_size
                    return (x_patches, y_patches)

            raise ValueError(
                "ColQwen2 requires batch_images with image_grid_thw for mean pooling. "
                "Ensure _process_images result is passed to _get_patches."
            )

        if self.model_type == ModelType.COLQWEN3:
            # ColQwen3: processor has get_n_patches with spatial_merge_size parameter
            merge_size = self.processor.image_processor.merge_size
            return self.processor.get_n_patches(
                image_size,
                spatial_merge_size=merge_size,
            )

        raise ValueError(f"Unknown model type: {self.model_type}")

    def _process_images(self, images: List[Image.Image]) -> Dict[str, Any]:
        """Process images into model inputs based on model type.

        Args:
            images: List of PIL Images to process

        Returns:
            Dict containing input_ids, attention_mask, pixel_values, etc.
        """
        if self.processor is None:
            raise RuntimeError("Processor not initialized")

        if self.model_type == ModelType.COLPALI:
            # ColPali: uses __call__ with images parameter
            return self.processor(images=images)

        if self.model_type == ModelType.COLQWEN2:
            # ColQwen2: uses __call__ with images parameter
            return self.processor(images=images)

        if self.model_type == ModelType.COLQWEN3:
            # ColQwen3: uses dedicated process_images method
            return self.processor.process_images(images=images)

        raise ValueError(f"Unknown model type: {self.model_type}")

    def _process_queries(self, texts: List[str]) -> Dict[str, Any]:
        """Process text queries into model inputs based on model type.

        Args:
            texts: List of query strings to process

        Returns:
            Dict containing input_ids, attention_mask, etc.
        """
        if self.processor is None:
            raise RuntimeError("Processor not initialized")

        if self.model_type == ModelType.COLPALI:
            # ColPali: uses __call__ with text parameter
            return self.processor(text=texts)

        if self.model_type == ModelType.COLQWEN2:
            # ColQwen2: uses __call__ with text parameter
            return self.processor(text=texts)

        if self.model_type == ModelType.COLQWEN3:
            # ColQwen3: uses dedicated process_texts method
            return self.processor.process_texts(texts=texts)

        raise ValueError(f"Unknown model type: {self.model_type}")

    def _get_embeddings_from_output(self, output: Any) -> torch.Tensor:
        """Extract embeddings from model output.

        All supported models (ColPali, ColQwen2, ColQwen3) return output objects
        with an .embeddings attribute containing the multi-vector embeddings.

        Args:
            output: Model forward pass output

        Returns:
            Tensor of shape (batch_size, sequence_length, embedding_dim)
        """
        # All HF ColVision models return objects with .embeddings attribute
        return output.embeddings

    def _get_image_token_id(self) -> int:
        """Get the image token ID used for identifying image tokens in input_ids.

        Used for mean pooling to identify which tokens correspond to image patches.

        Returns:
            Integer token ID for the image token
        """
        if self.processor is None:
            raise RuntimeError("Processor must be initialized")

        if self.model_type == ModelType.COLPALI:
            # ColPali: processor has image_token_id as direct attribute
            return self.processor.image_token_id

        if self.model_type == ModelType.COLQWEN2:
            # ColQwen2: image token is "<|image_pad|>", get ID from tokenizer
            # Token ID is 151655
            return self.processor.tokenizer.convert_tokens_to_ids("<|image_pad|>")

        if self.model_type == ModelType.COLQWEN3:
            # ColQwen3: processor has image_token_id as direct attribute
            # Token ID is 151655
            return self.processor.image_token_id

        raise ValueError(f"Unknown model type: {self.model_type}")

    def generate_embeddings(self, request: EmbedRequest) -> EmbedResponse:
        """
        Main inference function that processes EmbedRequest models.
        This is the main entry point called by the FastAPI wrapper.
        """
        try:
            # Check if model is initialized
            if not self.is_initialized or self.model is None or self.processor is None:
                raise RuntimeError("Model not initialized. Call initialize() first.")

            logger.debug("Processing inference request")

            # Determine request type and process accordingly
            if request.images:
                return self._process_images_from_request(request)
            elif request.texts:
                return self._process_texts_from_request(request)
            else:
                raise ValueError(
                    "Request must contain either 'images' or 'texts' field"
                )

        except Exception as e:
            logger.error("Inference failed: %s", str(e))
            raise

    def _process_images_from_request(
        self,
        request: EmbedRequest,
    ) -> EmbedResponse:
        """
        Process image embedding request with multiple pooling types.
        """
        try:
            # Validate that images field is not None (should be guaranteed by caller)
            if request.images is None:
                raise ValueError(
                    "Request must contain 'images' field with image content"
                )

            # Convert base64 strings to PIL images
            images = []
            for i, image_data in enumerate(request.images):
                try:
                    image = self.base64_to_image(image_data)
                    images.append(image)

                except Exception as e:
                    logger.error("Failed to process image at index %s: %s", i, str(e))
                    raise ValueError(
                        "Failed to process image at index %s: %s" % (i, str(e))
                    )

            logger.debug(f"Processing {len(images)} images with {self.model_type}")

            if self.processor is None or self.model is None:
                raise RuntimeError(
                    "Model and processor must be initialized before inference"
                )
            if self.device is None:
                raise RuntimeError("Device must be set before inference")

            with torch.no_grad():
                batch_images = self._process_images(images)

                # Move to device
                # Note: BatchFeature has .to() method, plain dict does not
                if hasattr(batch_images, "to"):
                    batch_images = batch_images.to(self.model.device)
                elif hasattr(batch_images, "items"):
                    # Handle dict-like objects (BatchFeature inherits from UserDict, not dict)
                    batch_images = {
                        k: v.to(self.model.device) if hasattr(v, "to") else v
                        for k, v in batch_images.items()
                    }

                output = self.model(**batch_images)
                embeddings = self._get_embeddings_from_output(output)

            # Optimize for common case of single pooling type
            pooling_types = set(request.pooling_type)  # Remove duplicates

            # Pre-compute tokenized_images if needed for mean pooling
            tokenized_images = None
            if PoolingType.MEAN_POOLING in pooling_types:
                tokenized_images = getattr(batch_images, "input_ids", None)
                if tokenized_images is None and hasattr(batch_images, "get"):
                    tokenized_images = batch_images.get("input_ids")
                if tokenized_images is None:
                    raise ValueError("Processed image batch missing input_ids tensor")

            response = {}
            for pooling_type in pooling_types:
                if pooling_type == PoolingType.MEAN_POOLING:
                    # Apply mean pooling to existing embeddings
                    logger.debug("Applying mean pooling...")

                    # Process all pooling operations on GPU first, then convert to CPU once
                    pooled_rows_tensors = []
                    pooled_cols_tensors = []

                    # tokenized_images is guaranteed to be non-None by pre-computation check
                    assert tokenized_images is not None

                    # Get image token ID based on model type
                    image_token_id = self._get_image_token_id()

                    for i, (image_embedding, tokenized_image, image) in enumerate(
                        zip(embeddings, tokenized_images, images)
                    ):
                        # Pass batch_images for ColQwen2 which needs image_grid_thw
                        # BatchFeature inherits from UserDict (has .get) but not dict
                        x_patches, y_patches = self._get_patches(
                            image.size,
                            batch_images=batch_images
                            if hasattr(batch_images, "get")
                            else None,
                            image_index=i,
                        )

                        image_tokens_mask = tokenized_image == image_token_id

                        embedding_dim = image_embedding.shape[-1]
                        image_tokens = image_embedding[image_tokens_mask].view(
                            x_patches, y_patches, embedding_dim
                        )
                        pooled_by_rows = torch.mean(image_tokens, dim=0)
                        pooled_by_columns = torch.mean(image_tokens, dim=1)

                        image_token_idxs = torch.nonzero(
                            image_tokens_mask.int(), as_tuple=False
                        )
                        first_image_token_idx = image_token_idxs[0].cpu().item()
                        last_image_token_idx = image_token_idxs[-1].cpu().item()

                        prefix_tokens = image_embedding[:first_image_token_idx]
                        postfix_tokens = image_embedding[last_image_token_idx + 1 :]

                        # Keep tensors on GPU during processing
                        pooled_by_rows_full = torch.cat(
                            (prefix_tokens, pooled_by_rows, postfix_tokens), dim=0
                        )
                        pooled_by_columns_full = torch.cat(
                            (prefix_tokens, pooled_by_columns, postfix_tokens), dim=0
                        )

                        pooled_rows_tensors.append(pooled_by_rows_full)
                        pooled_cols_tensors.append(pooled_by_columns_full)

                    # Single batch conversion to CPU/numpy/list
                    pooled_by_rows_batch = [
                        tensor.cpu().float().numpy().tolist()
                        for tensor in pooled_rows_tensors
                    ]
                    pooled_by_columns_batch = [
                        tensor.cpu().float().numpy().tolist()
                        for tensor in pooled_cols_tensors
                    ]

                    response["mean_row_pooled_embeddings"] = pooled_by_rows_batch
                    response["mean_column_pooled_embeddings"] = pooled_by_columns_batch

                    logger.debug("Mean pooling completed")

                elif pooling_type == PoolingType.HIERARCHICAL_POOLING:
                    # Apply hierarchical pooling to existing embeddings
                    logger.debug("Applying hierarchical pooling...")

                    pooled_embeddings = self._apply_hierarchical_pooling(
                        embeddings, request.pooling_config
                    )
                    # Convert once after pooling is complete
                    final_embeddings = pooled_embeddings.cpu().float().numpy().tolist()

                    response["hierarchical_pooled_embeddings"] = final_embeddings

                    logger.debug("Hierarchical pooling completed")

                elif pooling_type == PoolingType.NONE:
                    # No pooling - use original embeddings
                    logger.debug("Adding original embeddings...")

                    # Single conversion to CPU/numpy/list
                    final_embeddings = embeddings.cpu().float().numpy().tolist()
                    response["embeddings"] = final_embeddings

                    logger.debug("Original embeddings added")

            logger.info("Generated embeddings for %s images", len(request.images))
            return EmbedResponse(**response)

        except Exception as e:
            logger.error("Image processing failed: %s", str(e))
            raise

    def _process_texts_from_request(self, request: EmbedRequest) -> EmbedResponse:
        """
        Process text queries embedding request.
        Text queries return single embeddings, so pooling is not applicable.
        """
        try:
            # Validate that texts field is not None (should be guaranteed by caller)
            if request.texts is None:
                raise ValueError("Request must contain 'texts' field with text content")

            logger.debug("Processing %d text queries", len(request.texts))

            if self.processor is None or self.model is None:
                raise RuntimeError(
                    "Model and processor must be initialized before inference"
                )
            if self.device is None:
                raise RuntimeError("Device must be set before inference")

            with torch.no_grad():
                batch_queries = self._process_queries(request.texts)

                # Move to device
                # Note: BatchFeature has .to() method, plain dict does not
                if hasattr(batch_queries, "to"):
                    batch_queries = batch_queries.to(self.model.device)
                elif hasattr(batch_queries, "items"):
                    # Handle dict-like objects (BatchFeature inherits from UserDict, not dict)
                    batch_queries = {
                        k: v.to(self.model.device) if hasattr(v, "to") else v
                        for k, v in batch_queries.items()
                    }

                output = self.model(**batch_queries)
                query_embeddings = self._get_embeddings_from_output(output)
                final_embeddings = query_embeddings.cpu().float().numpy().tolist()

            response = {"embeddings": final_embeddings}

            logger.info("Generated embeddings for %s text queries", len(request.texts))
            return EmbedResponse(**response)

        except Exception as e:
            logger.error("Text queries processing failed: %s", str(e))
            raise


# Global instance
_inference_instance: Optional[ColPaliInference] = None


def get_inference_instance() -> ColPaliInference:
    """Get or create the global inference instance."""
    global _inference_instance
    if _inference_instance is None:
        _inference_instance = ColPaliInference()
    return _inference_instance
