# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""
ColQwen2 Model Inference Module

Handles model initialization and inference logic for Kubernetes deployment.
"""

import base64
import json
import logging
import os
from io import BytesIO
from typing import Any, Dict, Optional, Tuple, cast

import torch
from colpali_engine.compression.token_pooling import HierarchicalTokenPooler
from colpali_engine.models import ColQwen2, ColQwen2Processor
from PIL import Image
from transformers.utils.import_utils import is_flash_attn_2_available

from .models import EmbedRequest, EmbedResponse, PoolingType

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    force=True,
)
logger = logging.getLogger(__name__)


class ColQwen2Inference:
    """
    ColQwen2 model inference handler for embedding generation.
    Manages model lifecycle and provides inference capabilities for both images and text.
    """

    PROCESSOR_SUBDIR = "processor"
    MODEL_SUBDIR = "model"

    def __init__(self):
        """Initialize the inference handler."""
        self.model: Optional[ColQwen2] = None
        self.processor: Optional[ColQwen2Processor] = None
        self.device: Optional[torch.device] = None
        self.hierarchical_pooler: Optional[HierarchicalTokenPooler] = None
        self.is_initialized = False

        self.device_map = "auto"
        self.model_id = "vidore/colqwen2-v1.0"
        self.base_model_id = "vidore/colqwen2-base"
        self.model_directory = os.getenv("MODEL_DIRECTORY_PATH", "/tmp/model-directory")
        self.torch_dtype = None

    def _get_model_dir_name(self) -> str:
        """Get the model directory name from instance model_id."""
        return self.model_id.replace("/", "-")

    def _load_models_from_paths(
        self,
        processor_path: str,
        model_path: str,
        for_validation=False,
        device_map=None,
    ) -> Optional[Tuple[ColQwen2Processor, ColQwen2]]:
        """
        Load processor and model from given paths using instance configuration.

        Args:
            processor_path: Path to processor directory
            model_path: Path to model directory
            for_validation: If True, returns models for validation. If False, sets instance variables.
            device_map: Optional override for device mapping (uses instance default if None)

        Returns:
            If for_validation=True: tuple of (processor, model)
            If for_validation=False: None (sets instance variables)
        """
        effective_device_map = device_map if device_map is not None else self.device_map

        processor_result = ColQwen2Processor.from_pretrained(
            processor_path, local_files_only=True
        )
        if isinstance(processor_result, tuple):
            loaded_processor = processor_result[0]
        else:
            loaded_processor = processor_result
        adapter_config_path = os.path.join(model_path, "adapter_config.json")
        base_model_path = os.path.join(model_path, "base_model")

        if os.path.exists(adapter_config_path) and os.path.isdir(base_model_path):
            try:
                with open(adapter_config_path, "r", encoding="utf-8") as config_file:
                    adapter_config = json.load(config_file)

                if adapter_config.get("base_model_name_or_path") != base_model_path:
                    adapter_config["base_model_name_or_path"] = base_model_path
                    with open(
                        adapter_config_path, "w", encoding="utf-8"
                    ) as config_file:
                        json.dump(adapter_config, config_file, indent=2)
                    logger.debug(
                        "Updated adapter_config.json base_model path to %s",
                        base_model_path,
                    )
            except (json.JSONDecodeError, OSError) as error:
                logger.warning(
                    "Unable to update adapter_config.json at %s: %s",
                    adapter_config_path,
                    error,
                )
        else:
            logger.debug(
                "Adapter configuration or base model directory missing at %s",
                model_path,
            )

        use_flash_attention_2 = is_flash_attn_2_available()
        attn_implementation = "flash_attention_2" if use_flash_attention_2 else None

        model_kwargs = {
            "torch_dtype": self.torch_dtype,
            "local_files_only": True,
            "low_cpu_mem_usage": True,
            "use_safetensors": True,
        }

        if attn_implementation:
            model_kwargs["attn_implementation"] = attn_implementation

        if effective_device_map:
            model_kwargs["device_map"] = effective_device_map
        elif self.device and self.device.type == "cuda":
            model_kwargs["device_map"] = "cuda:0"

        loaded_model = ColQwen2.from_pretrained(model_path, **model_kwargs)

        if for_validation:
            return loaded_processor, loaded_model
        else:
            self.processor = loaded_processor
            self.model = loaded_model

            if (
                not effective_device_map or effective_device_map == "cpu"
            ) and self.device is not None:
                cast(Any, self.model).to(self.device)

            self.model.eval()
            self.hierarchical_pooler = HierarchicalTokenPooler()
            return None

    def _can_load_model(self, processor_path: str, model_path: str) -> bool:
        """
        Check if model can be loaded successfully from the given paths.
        Returns True if both processor and model can be loaded without errors.
        Performs full model loading to ensure integrity.
        """
        try:
            if not os.path.exists(processor_path) or not os.path.exists(model_path):
                logger.info(
                    "Model paths don't exist: processor=%s, model=%s",
                    os.path.exists(processor_path),
                    os.path.exists(model_path),
                )
                return False

            processor_config = os.path.join(processor_path, "tokenizer_config.json")
            adapter_config = os.path.join(model_path, "adapter_config.json")
            base_model_config = os.path.join(model_path, "base_model", "config.json")

            if (
                not os.path.exists(processor_config)
                or not os.path.exists(adapter_config)
                or not os.path.exists(base_model_config)
            ):
                logger.info(
                    "Model config files missing: processor_config=%s, adapter_config=%s, base_model_config=%s",
                    os.path.exists(processor_config),
                    os.path.exists(adapter_config),
                    os.path.exists(base_model_config),
                )
                return False

            logger.info(
                "Testing FULL model loading from: processor=%s, model=%s",
                processor_path,
                model_path,
            )

            validation_models = self._load_models_from_paths(
                processor_path,
                model_path,
                for_validation=True,
                device_map="cpu",
            )

            if validation_models is None:
                logger.debug("Validation loading returned no models")
                return False

            test_processor, test_model = validation_models

            logger.debug("Validation: Processor and model loaded successfully")

            del test_model, test_processor
            import gc

            gc.collect()

            logger.debug(
                "Model validation successful - model is complete and functional"
            )
            return True

        except Exception as e:
            logger.info("Full model loading test failed: %s - will re-download", str(e))
            return False

    def _download_model_if_needed(self, output_dir_path: Optional[str] = None):
        """Download ColQwen2 processor, base model, and adapter if missing."""
        import time
        from pathlib import Path

        output_path = (
            output_dir_path if output_dir_path is not None else self.model_directory
        )
        output_dir = Path(output_path)
        start_time = time.time()

        logger.info("Starting model download")
        logger.debug("Adapter ID: %s", self.model_id)
        logger.debug("Base model ID: %s", self.base_model_id)
        logger.debug("Output directory: %s", output_path)
        logger.debug("HF_HOME cache: %s", os.getenv("HF_HOME", "default"))

        model_dir_name = self._get_model_dir_name()
        model_base_dir = output_dir / model_dir_name
        processor_dir = model_base_dir / self.PROCESSOR_SUBDIR
        model_dir = model_base_dir / self.MODEL_SUBDIR
        base_model_dir = model_dir / "base_model"
        adapter_dir = model_dir / "adapter"

        logger.debug("Creating directory structure:")
        logger.debug("Base dir: %s", model_base_dir)
        logger.debug("Processor dir: %s", processor_dir)
        logger.debug("Model dir: %s", model_dir)

        processor_dir.mkdir(parents=True, exist_ok=True)
        model_dir.mkdir(parents=True, exist_ok=True)
        base_model_dir.mkdir(parents=True, exist_ok=True)
        adapter_dir.mkdir(parents=True, exist_ok=True)

        if self._can_load_model(str(processor_dir), str(model_dir)):
            logger.info("Model already exists and loadable - skipping download")
            logger.debug("Model location: %s", model_base_dir)
            return

        try:
            import shutil

            from huggingface_hub import snapshot_download
            from transformers import AutoModelForVision2Seq

            logger.info("Step 1/3: Downloading processor from %s", self.model_id)
            processor = ColQwen2Processor.from_pretrained(self.model_id)
            if isinstance(processor, tuple):
                processor = processor[0]
                logger.debug("Processor returned as tuple, using first element")

            processor.save_pretrained(str(processor_dir))
            logger.debug("Processor saved to %s", processor_dir)

            logger.info("Step 2/3: Downloading base model from %s", self.base_model_id)
            base_model = AutoModelForVision2Seq.from_pretrained(self.base_model_id)
            base_model.save_pretrained(str(base_model_dir))
            logger.debug("Base model saved to %s", base_model_dir)

            logger.info("Step 3/3: Downloading adapter from %s", self.model_id)
            snapshot_download(
                repo_id=self.model_id,
                allow_patterns=[
                    "adapter_config.json",
                    "adapter_model.safetensors",
                ],
                local_dir=str(adapter_dir),
                local_dir_use_symlinks=False,
            )

            for file_name in ["adapter_config.json", "adapter_model.safetensors"]:
                source = adapter_dir / file_name
                target = model_dir / file_name
                if source.exists():
                    shutil.copy2(source, target)
                    logger.debug("Copied %s to %s", source, target)
                else:
                    logger.warning("Expected adapter file %s missing", source)

            adapter_config_path = model_dir / "adapter_config.json"
            if adapter_config_path.exists():
                try:
                    with adapter_config_path.open("r", encoding="utf-8") as config_file:
                        adapter_config = json.load(config_file)
                    adapter_config.setdefault("base_model_name_or_path", "./base_model")
                    with adapter_config_path.open("w", encoding="utf-8") as config_file:
                        json.dump(adapter_config, config_file, indent=2)
                except (OSError, json.JSONDecodeError) as error:
                    logger.warning(
                        "Unable to normalise adapter_config.json at %s: %s",
                        adapter_config_path,
                        error,
                    )

            model_info = {
                "name": "colqwen2-v1.0",
                "adapter_name": self.model_id,
                "base_model_name": self.base_model_id,
                "processor_path": "processor",
                "model_path": "model",
                "architecture": "ColQwen2",
            }

            model_info_path = model_base_dir / "model_info.json"
            with model_info_path.open("w", encoding="utf-8") as info_file:
                json.dump(model_info, info_file, indent=2)
            logger.debug("Model info written to %s", model_info_path)

        except Exception as error:
            logger.error("Download failed: %s", error)
            raise

        total_time = time.time() - start_time
        logger.info("Download complete")
        logger.debug("Total time: %.2f minutes", total_time / 60)
        logger.debug("Model saved to: %s", model_base_dir)

    def _load_model_from_disk(self, processor_path: str, model_model_path: str):
        """Helper function to load model from disk using shared loading logic."""
        logger.debug("Loading processor from: %s", processor_path)
        logger.debug("Loading ColQwen2 model weights from: %s", model_model_path)

        # Determine device mapping based on instance device
        device_map = "auto" if self.device and self.device.type == "cuda" else None

        # Use shared loading logic for production
        self._load_models_from_paths(
            processor_path,
            model_model_path,
            for_validation=False,  # Sets instance variables
            device_map=device_map,
        )

        logger.info("Model loaded successfully")

    def initialize(self):
        """
        Initialize the ColQwen2 model and processor for Kubernetes deployment.
        Adapted from the original Azure ML version to work with Kubernetes volume mounts.
        """
        if self.is_initialized:
            logger.debug("Model already initialized")
            return

        logger.info("Initializing ColQwen2 model for inference")

        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.torch_dtype = (
            torch.bfloat16 if torch.cuda.is_available() else torch.float32
        )
        logger.info("Device: %s", self.device)

        model_dir_name = self._get_model_dir_name()
        model_base_path = os.path.join(self.model_directory, model_dir_name)
        processor_path = os.path.join(model_base_path, self.PROCESSOR_SUBDIR)
        model_model_path = os.path.join(model_base_path, self.MODEL_SUBDIR)

        logger.info("Loading pre-downloaded model from InitContainer...")

        try:
            self._load_model_from_disk(processor_path, model_model_path)
            logger.info("ColQwen2 model initialization completed successfully")
            self.is_initialized = True

        except Exception as e:
            logger.error("Failed to load pre-downloaded model: %s", str(e))
            logger.error("Model base path: %s", model_base_path)

            raise RuntimeError(
                f"InitContainer should have pre-downloaded the model to {model_base_path}. "
                "Check InitContainer logs for download failures."
            )

    def base64_to_image(self, base64_string: str):
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
            logger.info("Original embeddings shape: %s", original_shape)

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
            logger.info("Pooled embeddings shape: %s", pooled_shape)

            if len(original_shape) >= 3 and len(pooled_shape) >= 3:
                original_size = original_shape[1] * original_shape[2]
                pooled_size = pooled_shape[1] * pooled_shape[2]
                if pooled_size > 0:
                    compression_ratio = original_size / pooled_size
                    logger.info(
                        "Pooling completed - compression ratio: %.2fx (from %d to %d elements)",
                        compression_ratio,
                        original_size,
                        pooled_size,
                    )

            return pooled_tensor

        except Exception as e:
            logger.error("Hierarchical pooling failed: %s", str(e))
            raise

    def _get_patches(self, image_size):
        """Get the number of patches for x and y dimensions."""
        # This code is taken from the example at: https://qdrant.tech/documentation/advanced-tutorials/pdf-retrieval-at-scale/

        if self.processor is None or self.model is None:
            raise RuntimeError(
                "Model and processor must be initialized before computing patches"
            )

        spatial_merge_size = getattr(self.model, "spatial_merge_size", None)

        if (spatial_merge_size is None) and hasattr(self.model, "model"):
            base_model = getattr(self.model, "model")
            spatial_merge_size = getattr(
                base_model, "spatial_merge_size", spatial_merge_size
            )

        if spatial_merge_size is None:
            raise AttributeError(
                "ColQwen2 model missing patch metadata required for pooling"
            )

        return self.processor.get_n_patches(
            image_size,
            spatial_merge_size=spatial_merge_size,
        )

    def generate_embeddings(self, request: EmbedRequest) -> EmbedResponse:
        """
        Main inference function that processes EmbedRequest models.
        This is the main entry point called by the FastAPI wrapper.
        """
        try:
            # Check if model is initialized
            if not self.is_initialized or self.model is None or self.processor is None:
                raise RuntimeError("Model not initialized. Call initialize() first.")

            logger.info("Processing inference request")

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

            logger.info("Processing images with ColQwen2...")

            if self.processor is None or self.model is None:
                raise RuntimeError(
                    "Model and processor must be initialized before inference"
                )
            if self.device is None:
                raise RuntimeError("Device must be set before inference")

            with torch.no_grad():
                batch_images = self.processor.process_images(images)

                if hasattr(batch_images, "to"):
                    batch_images = batch_images.to(self.device)
                elif isinstance(batch_images, dict):
                    batch_images = {
                        k: v.to(self.device) if hasattr(v, "to") else v
                        for k, v in batch_images.items()
                    }

                embeddings = self.model(**batch_images)

            # Optimize for common case of single pooling type
            pooling_types = set(request.pooling_type)  # Remove duplicates

            # Pre-compute tokenized_images if needed for mean pooling
            tokenized_images = None
            if PoolingType.MEAN_POOLING in pooling_types:
                tokenized_images = getattr(batch_images, "input_ids", None)
                if tokenized_images is None and isinstance(batch_images, dict):
                    tokenized_images = batch_images.get("input_ids")
                if tokenized_images is None:
                    raise ValueError("Processed image batch missing input_ids tensor")

            response = {}
            for pooling_type in pooling_types:
                if pooling_type == PoolingType.MEAN_POOLING:
                    # Apply mean pooling to existing embeddings
                    logger.debug("Applying mean pooling...")

                    pooled_by_rows_batch = []
                    pooled_by_columns_batch = []

                    # Process all pooling operations on GPU first, then convert to CPU once
                    pooled_rows_tensors = []
                    pooled_cols_tensors = []

                    # tokenized_images is guaranteed to be non-None by pre-computation check
                    assert tokenized_images is not None
                    for i, (image_embedding, tokenized_image, image) in enumerate(
                        zip(embeddings, tokenized_images, images)
                    ):
                        x_patches, y_patches = self._get_patches(image.size)

                        image_tokens_mask = (
                            tokenized_image == self.processor.image_token_id
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
                    logger.info("Adding original embeddings...")

                    # Single conversion to CPU/numpy/list
                    final_embeddings = embeddings.cpu().float().numpy().tolist()
                    response["embeddings"] = final_embeddings

                    logger.info("Original embeddings added")

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

            logger.info("Processing %s text queries", len(request.texts))

            logger.info("Processing text queries...")

            if self.processor is None or self.model is None:
                raise RuntimeError(
                    "Model and processor must be initialized before inference"
                )
            if self.device is None:
                raise RuntimeError("Device must be set before inference")

            with torch.no_grad():
                batch_queries = self.processor.process_queries(request.texts)

                if hasattr(batch_queries, "to"):
                    batch_queries = batch_queries.to(self.device)
                elif isinstance(batch_queries, dict):
                    batch_queries = {
                        k: v.to(self.device) if hasattr(v, "to") else v
                        for k, v in batch_queries.items()
                    }

                query_embeddings = self.model(**batch_queries)
                final_embeddings = query_embeddings.cpu().float().numpy().tolist()

            response = {"embeddings": final_embeddings}

            logger.info("Generated embeddings for %s text queries", len(request.texts))
            return EmbedResponse(**response)

        except Exception as e:
            logger.error("Text queries processing failed: %s", str(e))
            raise


# Global instance for backward compatibility
_inference_instance: Optional[ColQwen2Inference] = None


def get_inference_instance() -> ColQwen2Inference:
    """Get or create the global inference instance."""
    global _inference_instance
    if _inference_instance is None:
        _inference_instance = ColQwen2Inference()
    return _inference_instance
