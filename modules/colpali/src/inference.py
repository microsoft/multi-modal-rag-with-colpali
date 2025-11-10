# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""
ColQwen2 Model Inference Module

Handles model initialization and inference logic for Kubernetes deployment.
"""

import base64
import logging
import os
from io import BytesIO
from typing import Any, Dict, Optional

import torch
from colpali_engine.compression.token_pooling import HierarchicalTokenPooler
from PIL import Image
from transformers import ColQwen2ForRetrieval, ColQwen2Processor
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
        self.model: Optional[ColQwen2ForRetrieval] = None
        self.processor: Optional[ColQwen2Processor] = None
        self.device: Optional[torch.device] = None
        self.hierarchical_pooler: Optional[HierarchicalTokenPooler] = None
        self.is_initialized = False

        self.device_map = "auto"
        self.model_id = os.getenv("MODEL_ID", "vidore/colqwen2-v1.0-hf")
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
    ):
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
        use_flash_attention_2 = is_flash_attn_2_available()
        attn_implementation = "flash_attention_2" if use_flash_attention_2 else "sdpa"

        model_kwargs = {
            "torch_dtype": self.torch_dtype,
            "local_files_only": True,
            "attn_implementation": attn_implementation,
            "low_cpu_mem_usage": True,
            "use_safetensors": True,
            "trust_remote_code": True,
        }

        if effective_device_map:
            model_kwargs["device_map"] = effective_device_map

        loaded_model = ColQwen2ForRetrieval.from_pretrained(model_path, **model_kwargs)

        if for_validation:
            return loaded_processor, loaded_model
        else:
            self.processor = loaded_processor
            self.model = loaded_model

            if (
                not effective_device_map or effective_device_map == "cpu"
            ) and self.device is not None:
                self.model = self.model.to(self.device)

            self.model.eval()
            self.hierarchical_pooler = HierarchicalTokenPooler()

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
            model_config = os.path.join(model_path, "config.json")

            if not os.path.exists(processor_config) or not os.path.exists(model_config):
                logger.info(
                    "Model config files missing: processor_config=%s, model_config=%s",
                    os.path.exists(processor_config),
                    os.path.exists(model_config),
                )
                return False

            logger.info(
                "Testing FULL model loading from: processor=%s, model=%s",
                processor_path,
                model_path,
            )

            test_processor, test_model = self._load_models_from_paths(
                processor_path,
                model_path,
                for_validation=True,
                device_map="cpu",
            )

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

    def _download_model_if_needed(self, output_dir_path: str = None):
        """Download ColQwen2 model if it doesn't exist locally."""
        import time
        from pathlib import Path

        output_path = (
            output_dir_path if output_dir_path is not None else self.model_directory
        )
        output_dir = Path(output_path)
        start_time = time.time()

        logger.info("Starting model download")
        logger.debug("Model ID: %s", self.model_id)
        logger.debug("Output directory: %s", output_path)
        logger.debug("HF_HOME cache: %s", os.getenv("HF_HOME", "default"))

        model_dir_name = self._get_model_dir_name()
        model_base_dir = output_dir / model_dir_name
        processor_dir = model_base_dir / self.PROCESSOR_SUBDIR
        model_dir = model_base_dir / self.MODEL_SUBDIR

        logger.debug("Creating directory structure:")
        logger.debug("Base dir: %s", model_base_dir)
        logger.debug("Processor dir: %s", processor_dir)
        logger.debug("Model dir: %s", model_dir)

        processor_dir.mkdir(parents=True, exist_ok=True)
        model_dir.mkdir(parents=True, exist_ok=True)

        if self._can_load_model(str(processor_dir), str(model_dir)):
            logger.info("Model already exists and loadable - skipping download")
            logger.debug("Model location: %s", model_base_dir)
            return
        if processor_dir.exists() or model_dir.exists():
            if not self._can_load_model(str(processor_dir), str(model_dir)):
                logger.info("Cleaning up partial/corrupted downloads...")
                import shutil

                if processor_dir.exists():
                    shutil.rmtree(processor_dir)
                    logger.debug("Cleaned up processor directory")
                if model_dir.exists():
                    shutil.rmtree(model_dir)
                    logger.debug("Cleaned up model directory")
                # Recreate directories
                processor_dir.mkdir(parents=True, exist_ok=True)
                model_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Download and save processor
            logger.info("Step 1/2: Downloading processor...")
            processor = ColQwen2Processor.from_pretrained(self.model_id)
            if isinstance(processor, tuple):
                processor = processor[0]
                logger.debug("Processor returned as tuple, using first element")

            logger.debug("Saving processor to: %s", processor_dir)
            processor.save_pretrained(str(processor_dir))
            logger.debug("Processor downloaded successfully")

            # Download and save model
            logger.info(
                "Step 2/2: Downloading model (this may take several minutes)..."
            )
            use_flash_attention_2 = is_flash_attn_2_available()
            attn_implementation = (
                "flash_attention_2" if use_flash_attention_2 else "sdpa"
            )
            logger.debug("Using attention implementation: %s", attn_implementation)

            model = ColQwen2ForRetrieval.from_pretrained(
                self.model_id,
                torch_dtype=torch.bfloat16,  # Use bfloat16 for download regardless of device
                device_map="cpu",
                attn_implementation=attn_implementation,
                low_cpu_mem_usage=True,
                use_safetensors=True,
                trust_remote_code=True,
            )
            logger.debug("Saving model to: %s", model_dir)
            model.save_pretrained(str(model_dir))
            logger.debug("Model downloaded successfully")

        except Exception as e:
            logger.error("Download failed: %s", str(e))
            # Clean up partial downloads on failure to prevent disk space issues
            logger.info("Cleaning up partial downloads due to failure...")
            if processor_dir.exists():
                import shutil

                shutil.rmtree(processor_dir)
                logger.debug("Cleaned up partial processor download")
            if model_dir.exists():
                import shutil

                shutil.rmtree(model_dir)
                logger.debug("Cleaned up partial model download")
            raise

        total_time = time.time() - start_time
        logger.info("Download complete")
        logger.debug("Total time: %.2f minutes", total_time / 60)
        logger.debug("Model saved to: %s", model_base_dir)

    def _load_model_from_disk(self, processor_path: str, model_model_path: str):
        """Helper function to load model from disk using shared loading logic."""
        logger.debug("Loading processor from: %s", processor_path)
        logger.debug("Loading ColQwen2 merged model from: %s", model_model_path)

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


# Helper functions from k8s_score.py
def base64_to_image(base64_string):
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
        """
        Apply hierarchical token pooling to embeddings using ColPali's implementation.
        """
        try:
            logger.debug("Applying hierarchical token pooling...")

            # Get original shape
            original_shape = embeddings.shape
            logger.info("Original embeddings shape: %s", original_shape)

            # Default pooling configuration
            pool_factor = 2
            if pooling_config and "pool_factor" in pooling_config:
                pool_factor = pooling_config["pool_factor"]

            # Apply pooling using the hierarchical pooler
            pooled_embeddings = self.hierarchical_pooler.forward(
                embeddings, pool_factor=pool_factor
            )

            # Get pooled shape
            pooled_shape = pooled_embeddings.shape
            logger.info("Pooled embeddings shape: %s", pooled_shape)

            # Calculate compression ratio
            original_size = original_shape[1] * original_shape[2]  # patches * dim
            pooled_size = pooled_shape[1] * pooled_shape[2]  # pooled_patches * dim
            compression_ratio = original_size / pooled_size

            logger.info(
                "Pooling completed - compression ratio: %.2fx (from %d to %d elements)",
                compression_ratio,
                original_size,
                pooled_size,
            )

            return pooled_embeddings

        except Exception as e:
            logger.error("Hierarchical pooling failed: %s", str(e))
            raise

    def _get_patches(self, image_size):
        """Get the number of patches for x and y dimensions."""
        # This code is taken from the example at: https://qdrant.tech/documentation/advanced-tutorials/pdf-retrieval-at-scale/
        return self.processor.get_n_patches(
            image_size,
            patch_size=self.model.patch_size,
            spatial_merge_size=self.model.spatial_merge_size,
        )

    def generate_embeddings(self, request: EmbedRequest) -> Dict[str, Any]:
        """
        Main inference function that processes EmbedRequest models.
        This is the main entry point called by the FastAPI wrapper.
        """
        try:
            # Check if model is initialized
            if not self.is_initialized or self.model is None or self.processor is None:
                raise RuntimeError("Model not initialized. Call initialize() first.")

            logger.info("Processing inference request")

            # Extract pooling configuration (already validated by Pydantic)
            pooling_types = request.pooling_type

            if not pooling_types:
                # Default to appropriate pooling based on request type
                if request.images:
                    logger.info(
                        "No valid pooling types specified for images, defaulting to 'hierarchical'"
                    )
                    pooling_types = [PoolingType.HIERARCHICAL]
                else:
                    logger.info(
                        "No valid pooling types specified for text, defaulting to 'none'"
                    )
                    pooling_types = [PoolingType.NONE]

            pooling_config = request.pooling_config

            # Update request with normalized pooling types
            request.pooling_type = pooling_types

            # Determine request type and process accordingly
            if request.images:
                return self._process_images_from_request(request, pooling_config)
            elif request.texts:
                # Text processing doesn't use pooling types since text returns single embeddings
                return self._process_texts_from_request(request)
            else:
                raise ValueError(
                    "Request must contain either 'images' or 'texts' field"
                )

        except Exception as e:
            logger.error("Inference failed: %s", str(e))
            error_response = {"error": str(e), "status": "error"}
            return error_response

    def _process_images_from_request(
        self,
        request: EmbedRequest,
        pooling_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Process image embedding request with multiple pooling types.
        """
        try:
            # Extract image data from request
            image_data_list = request.images
            if not image_data_list:
                raise ValueError(
                    "Request must contain 'images' field with image content"
                )

            if not isinstance(image_data_list, list):
                raise ValueError("'images' field must be a list")

            if len(image_data_list) == 0:
                raise ValueError("'images' list cannot be empty")

            # Convert base64 strings to PIL images
            images = []
            for i, image_data in enumerate(image_data_list):
                try:
                    # Handle direct base64 string or data URI
                    if isinstance(image_data, str):
                        base64_string = image_data
                    else:
                        raise ValueError("Invalid image data format at index %s" % i)

                    image = base64_to_image(base64_string)
                    images.append(image)

                except Exception as e:
                    logger.error("Failed to process image at index %s: %s", i, str(e))
                    raise ValueError(
                        "Failed to process image at index %s: %s" % (i, str(e))
                    )

            logger.info("Processing images with ColQwen2...")

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

            response = {}
            for pooling_type in request.pooling_type:
                if pooling_type == PoolingType.MEAN_POOLING:
                    # Apply mean pooling to existing embeddings
                    logger.debug("Applying mean pooling...")

                    pooled_by_rows_batch = []
                    pooled_by_columns_batch = []
                    original_embeddings = embeddings.cpu().float().numpy().tolist()

                    for i, (image_embedding, tokenized_image, image) in enumerate(
                        zip(embeddings, batch_images.input_ids, images)
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

                        pooled_by_rows = (
                            torch.cat(
                                (prefix_tokens, pooled_by_rows, postfix_tokens), dim=0
                            )
                            .cpu()
                            .float()
                            .numpy()
                            .tolist()
                        )
                        pooled_by_columns = (
                            torch.cat(
                                (prefix_tokens, pooled_by_columns, postfix_tokens),
                                dim=0,
                            )
                            .cpu()
                            .float()
                            .numpy()
                            .tolist()
                        )

                        pooled_by_rows_batch.append(pooled_by_rows)
                        pooled_by_columns_batch.append(pooled_by_columns)

                    response["mean_row_pooled_embeddings"] = {
                        "mean_pooling": pooled_by_rows_batch
                    }
                    response["mean_column_pooled_embeddings"] = {
                        "mean_pooling": pooled_by_columns_batch
                    }
                    # Also add original embeddings if not already added
                    if "embeddings" not in response:
                        response["embeddings"] = original_embeddings

                    logger.debug("Mean pooling completed")

                elif pooling_type == PoolingType.HIERARCHICAL:
                    # Apply hierarchical pooling to existing embeddings
                    logger.debug("Applying hierarchical pooling...")

                    if pooling_config is None:
                        raise ValueError(
                            "Hierarchical pooling requires pooling_config to be provided"
                        )

                    pooled_embeddings = self._apply_hierarchical_pooling(
                        embeddings, pooling_config
                    )
                    final_embeddings = pooled_embeddings.cpu().float().numpy().tolist()

                    response["hierarchical_pooled_embeddings"] = {
                        "hierarchical": final_embeddings
                    }

                    logger.debug("Hierarchical pooling completed")

                elif pooling_type == PoolingType.NONE:
                    # No pooling - use original embeddings
                    logger.info("Adding original embeddings...")

                    final_embeddings = embeddings.cpu().float().numpy().tolist()
                    response["embeddings"] = final_embeddings

                    logger.info("Original embeddings added")

            logger.info("Generated embeddings for %s images", len(images))

            # Create response with the new structure
            return EmbedResponse(**response)

        except Exception as e:
            logger.error("Image processing failed: %s", str(e))
            raise

    def _process_texts_from_request(self, request: EmbedRequest) -> Dict[str, Any]:
        """
        Process text queries embedding request.
        Text queries return single embeddings, so pooling is not applicable.
        """
        try:
            # Extract text queries from request
            text_queries = request.texts
            if not text_queries:
                raise ValueError("Request must contain 'texts' field with text content")

            if not isinstance(text_queries, list):
                raise ValueError("'texts' field must be a list")

            if len(text_queries) == 0:
                raise ValueError("'texts' list cannot be empty")

            logger.info("Processing %s text queries", len(text_queries))

            # Validate text queries
            for i, text_query in enumerate(text_queries):
                if not isinstance(text_query, str):
                    raise ValueError(f"Text query at index {i} must be a string")
                if len(text_query.strip()) == 0:
                    raise ValueError(f"Text query at index {i} cannot be empty")

            logger.info("Processing text queries...")

            with torch.no_grad():
                batch_queries = self.processor.process_queries(text_queries)

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

            logger.info("Generated embeddings for %s text queries", len(text_queries))
            return response

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


def init_model():
    """Initialize the model (backward compatibility function)."""
    instance = get_inference_instance()
    instance.initialize()


def generate_embeddings(request: EmbedRequest) -> Dict[str, Any]:
    """Generate embeddings (backward compatibility function)."""
    instance = get_inference_instance()
    return instance.generate_embeddings(request)
