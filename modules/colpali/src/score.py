"""
ColQwen2 Scoring Script for Azure ML Online Endpoint

This script handles inference requests for ColQwen2 document embeddings.
ColQwen2 is based on Qwen2-VL-2B-Instruct with ColBERT strategy for visual document retrieval.
It receives base64-encoded images and returns their ColQwen2 embeddings with optional hierarchical pooling.
"""

import base64
import json
import logging
import os
from enum import Enum
from io import BytesIO
from typing import Any, Dict, Optional

import torch
from colpali_engine.compression.token_pooling import HierarchicalTokenPooler
from colpali_engine.models import ColQwen2, ColQwen2Processor
from PIL import Image
from transformers.utils.import_utils import is_flash_attn_2_available

# Configure logging for Azure ML environment
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    force=True,  # Override any existing configuration
)
logger = logging.getLogger(__name__)


class PoolingType(Enum):
    """Enumeration for different pooling types."""

    NONE = "none"
    HIERARCHICAL = "hierarchical"


def init():
    """
    Initialize the ColQwen2 model and processor.
    This function is called once when the endpoint starts up.
    Model is loaded from the registered model path provided by Azure ML.
    """
    global model, processor, device, hierarchical_pooler

    logger.info("Initializing ColQwen2 model from registered model...")

    # Set device
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    logger.info("Using device: %s", device)

    # Get model path from environment variable set by Azure ML
    model_base_path = os.getenv("AZUREML_MODEL_DIR", "./model")
    # The model is in the colqwen2_model subdirectory (pipeline output name)
    model_path = os.path.join(model_base_path, "colqwen2_model")
    processor_path = os.path.join(model_path, "processor")
    model_model_path = os.path.join(model_path, "model")

    logger.info("Loading model from: %s", model_path)

    torch_dtype = torch.bfloat16 if torch.cuda.is_available() else torch.float32

    try:
        # Load processor from registered model with error handling for size configuration
        logger.info("Loading processor from: %s", processor_path)
        processor_result = ColQwen2Processor.from_pretrained(
            processor_path, local_files_only=True
        )

        # Handle tuple return from ColQwen2Processor.from_pretrained
        if isinstance(processor_result, tuple):
            processor = processor_result[0]
            logger.info("Extracted processor from tuple, type: %s", type(processor))
        else:
            processor = processor_result
            logger.info("Processor loaded directly, type: %s", type(processor))

        # Load the base model first from the base_model directory
        base_model_path = os.path.join(model_model_path, "base_model")
        logger.info("Loading base model from: %s", base_model_path)

        # Load ColQwen2 model with PEFT adapter
        # The model directory contains both the adapter files and the base_model subdirectory
        logger.info("Loading ColQwen2 model with adapter from: %s", model_model_path)
        logger.info("Base model path: %s", base_model_path)

        logging.info("Flash Attention 2 available: %s", is_flash_attn_2_available())

        # Fix the adapter config path before loading
        # The adapter_config.json has a relative path "./base_model" that needs to be resolved
        # to the actual deployment path
        adapter_config_path = os.path.join(model_model_path, "adapter_config.json")
        if os.path.exists(adapter_config_path):
            logger.info("Fixing adapter config path at: %s", adapter_config_path)
            with open(adapter_config_path, "r") as f:
                adapter_config = json.load(f)

            absolute_base_path = os.path.join(model_model_path, "base_model")
            logger.info(
                "Updating base_model path from './base_model' to: %s",
                absolute_base_path,
            )
            adapter_config["base_model_name_or_path"] = absolute_base_path

            # Write back the updated config
            with open(adapter_config_path, "w") as f:
                json.dump(adapter_config, f, indent=2)
            logger.info("Adapter config updated")

        # Load ColQwen2 model - it should now correctly load the adapter with absolute paths
        logger.info("Loading ColQwen2 from: %s", model_model_path)

        model = ColQwen2.from_pretrained(
            model_model_path,
            torch_dtype=torch_dtype,
            device_map="cuda:0" if torch.cuda.is_available() else None,
            attn_implementation="flash_attention_2"
            if is_flash_attn_2_available()
            else None,
            local_files_only=True,
        )

        logger.info("ColQwen2 loaded")

        # Check if the model has PEFT adapters loaded
        if hasattr(model, "peft_config"):
            logger.info("PEFT adapter detected in model")
            logger.info(
                "PEFT config keys: %s",
                list(model.peft_config.keys()) if model.peft_config else "None",
            )
        else:
            logger.warning(
                "No PEFT adapter detected - model may not have LoRA weights loaded!"
            )

        # Log model configuration details
        if hasattr(model, "config"):
            logger.info("Model config type: %s", type(model.config).__name__)

        model = model.eval()
        logger.info("Model set to evaluation mode")

        # Initialize the hierarchical token pooler (following ColPali example)
        hierarchical_pooler = HierarchicalTokenPooler()
        logger.info("Hierarchical token pooler initialized")

        logger.info("ColQwen2 model initialization completed successfully")

    except Exception as e:
        logger.error("Failed to initialize ColQwen2 model: %s", str(e))
        logger.error("Model path: %s", model_path)
        logger.error(
            "Available files: %s",
            os.listdir(model_path)
            if os.path.exists(model_path)
            else "Path does not exist",
        )
        raise


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


def apply_hierarchical_pooling(
    embeddings: torch.Tensor, pooling_config: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Apply hierarchical token pooling to embeddings using ColPali's implementation.
    Follows the exact pattern from the ColPali example.

    Args:
        embeddings: Tensor of embeddings from ColQwen2 model
        pooling_config: Configuration for pooling (e.g., pool_factor)

    Returns:
        Dict containing original embeddings and pooled embeddings
    """
    try:
        # Default pooling configuration
        # 3 is recommended by ColPali team: https://github.com/illuin-tech/colpali?tab=readme-ov-file#token-pooling
        if pooling_config is None:
            pooling_config = {"pool_factor": 3}

        pool_factor = pooling_config.get("pool_factor", 3)

        logger.info(f"Applying hierarchical pooling with pool_factor={pool_factor}")

        # Store original embeddings for response
        original_embeddings_list = embeddings.cpu().float().numpy().tolist()

        # Apply hierarchical pooling exactly as in the ColPali example
        pooled_embeddings = hierarchical_pooler.pool_embeddings(
            embeddings, pool_factor=pool_factor, padding=True, padding_side="right"
        )

        # Convert pooled embeddings to list format for JSON serialization
        # The pooler should return a tensor according to the ColPali example
        if torch.is_tensor(pooled_embeddings):
            pooled_embeddings_list = pooled_embeddings.cpu().float().numpy().tolist()
            pooled_shape = list(pooled_embeddings.shape)
            pooled_seq_len = (
                pooled_embeddings.size(1) if pooled_embeddings.dim() >= 2 else 1
            )
        else:
            # Fallback if unexpected return type
            logger.warning(
                f"Unexpected pooled embeddings type: {type(pooled_embeddings)}"
            )
            pooled_embeddings_list = original_embeddings_list
            pooled_shape = list(embeddings.shape)
            pooled_seq_len = embeddings.size(1) if embeddings.dim() >= 2 else 1

        # Calculate compression ratio based on sequence length (second dimension)
        original_seq_len = embeddings.size(1) if embeddings.dim() >= 2 else 1
        compression_ratio = (
            original_seq_len / pooled_seq_len if pooled_seq_len > 0 else 1.0
        )

        return {
            "original_embeddings": original_embeddings_list,
            "pooled_embeddings": pooled_embeddings_list,
            "pool_factor": pool_factor,
            "compression_ratio": compression_ratio,
            "original_shape": list(embeddings.shape),
            "pooled_shape": pooled_shape,
        }

    except Exception as e:
        logger.error(f"Error in hierarchical pooling: {e}")
        # Return original embeddings if pooling fails
        original_embeddings_list = (
            [
                embeddings[i].cpu().float().numpy().tolist()
                for i in range(embeddings.size(0))
            ]
            if embeddings.dim() == 3
            else [embeddings.cpu().float().numpy().tolist()]
        )
        return {
            "original_embeddings": original_embeddings_list,
            "pooled_embeddings": original_embeddings_list,  # Same as original if pooling fails
            "cluster_mappings": None,
            "pool_factor": 1,
            "compression_ratio": 1.0,
            "error": str(e),
        }


def run(raw_data):
    """
    Process inference request and return ColQwen2 embeddings.
    Supports both image and text query processing with optional hierarchical pooling.

    Args:
        raw_data (str): JSON string containing the request data
                       Format for images: {
                           "images": ["base64_string1", "base64_string2"],
                           "pooling_type": "hierarchical" | "none",  # optional
                           "pooling_config": {"pool_factor": 4}  # optional
                       }
                       Format for texts: {
                           "texts": ["query text 1", "query text 2"],
                           "pooling_type": "hierarchical" | "none",  # optional
                           "pooling_config": {"pool_factor": 4}  # optional
                       }

    Returns:
        str: JSON string containing the embeddings response
    """
    try:
        logger.info("Processing inference request")

        # Parse input data
        data = json.loads(raw_data)

        # Extract pooling configuration
        pooling_type_str = data.get("pooling_type", "none")
        try:
            pooling_type = PoolingType(pooling_type_str)
        except ValueError:
            logger.warning(
                f"Invalid pooling type '{pooling_type_str}', defaulting to 'none'"
            )
            pooling_type = PoolingType.NONE

        pooling_config = data.get("pooling_config", None)

        # Determine request type and process accordingly
        if "images" in data:
            return process_images(data, pooling_type, pooling_config)
        elif "texts" in data:
            return process_texts(data, pooling_type, pooling_config)
        else:
            raise ValueError("Request must contain either 'images' or 'texts' field")

    except Exception as e:
        logger.error("Inference failed: %s", str(e))
        error_response = {"error": str(e), "status": "error"}
        return error_response


def process_images(
    data: Dict[str, Any],
    pooling_type: PoolingType,
    pooling_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Process image embeddings request with optional hierarchical pooling.

    Args:
        data: Request data containing images
        pooling_type: Type of pooling to apply
        pooling_config: Configuration for pooling

    Returns:
        dict: Response with image embeddings
    """
    try:
        # Extract images from request
        if "images" not in data:
            raise ValueError("Request must contain 'images' field")

        image_data_list = data["images"]
        if not isinstance(image_data_list, list):
            raise ValueError("'images' field must be a list")

        if len(image_data_list) == 0:
            raise ValueError("'images' list cannot be empty")

        logger.info("Processing %s images", len(image_data_list))

        # Convert base64 strings to PIL images
        images = []
        for i, image_data in enumerate(image_data_list):
            try:
                if isinstance(image_data, dict) and "data" in image_data:
                    # Handle structured format: {"data": "base64_string"}
                    base64_string = image_data["data"]
                elif isinstance(image_data, str):
                    # Handle direct base64 string
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

        # Process images using ColQwen2Processor
        logger.info("Processing images with ColQwen2...")

        logger.info("Generating ColQwen2 image embeddings...")

        with torch.no_grad():
            # Process images using ColQwen2Processor - following HF documentation pattern
            batch_images = processor.process_images(images)

            # Move tensors to the correct device
            if hasattr(batch_images, "to"):
                batch_images = batch_images.to(device)
            elif isinstance(batch_images, dict):
                batch_images = {
                    k: v.to(device) if hasattr(v, "to") else v
                    for k, v in batch_images.items()
                }

            # Generate embeddings
            embeddings = model(**batch_images)

            # Log embedding dimensions for debugging
            logger.info(
                "Generated embeddings shape: %s (batch_size, num_patches, embedding_dim)",
                embeddings.shape,
            )
            logger.info(
                "Embedding dimensions: batch=%d, patches=%d, dim=%d",
                embeddings.shape[0],
                embeddings.shape[1],
                embeddings.shape[2],
            )

            # Prepare embeddings result
            embeddings_result = {
                "original_embeddings": embeddings.cpu().float().numpy().tolist()
            }

            # Apply hierarchical pooling if requested
            if pooling_type == PoolingType.HIERARCHICAL:
                pooling_result = apply_hierarchical_pooling(embeddings, pooling_config)
                embeddings_result.update(pooling_result)
                logger.info(
                    "After pooling - original shape: %s, pooled shape: %s, compression: %.2fx",
                    pooling_result.get("original_shape"),
                    pooling_result.get("pooled_shape"),
                    pooling_result.get("compression_ratio", 1.0),
                )
            else:
                # No pooling requested
                embeddings_result["pooled_embeddings"] = None
                embeddings_result["pool_factor"] = 1
                embeddings_result["compression_ratio"] = 1.0

        logger.info("Generated embeddings for %s images", len(images))

        # Prepare response
        response = {
            "embeddings": embeddings_result,
            "num_images": len(images),
            "pooling_type": pooling_type.value,
            "model": "vidore/colqwen2-v1.0",
            "input_type": "images",
            "status": "success",
        }

        return response

    except Exception as e:
        logger.error("Image processing failed: %s", str(e))
        raise


def process_texts(
    data: Dict[str, Any],
    pooling_type: PoolingType,
    pooling_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Process text queries embedding request with optional hierarchical pooling.

    Args:
        data: Request data containing text queries
        pooling_type: Type of pooling to apply
        pooling_config: Configuration for pooling

    Returns:
        dict: Response with text query embeddings
    """
    try:
        # Extract text queries from request
        text_queries = data.get("texts")
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

        logger.info(
            "Processing text queries: %s",
            [q[:50] + "..." if len(q) > 50 else q for q in text_queries],
        )

        # Process texts using ColQwen2Processor
        logger.info("Generating ColQwen2 text embeddings...")

        with torch.no_grad():
            # Process text queries using ColQwen2Processor
            # For text queries, we need to format them properly for the model
            batch_queries = processor.process_queries(text_queries)

            # Move tensors to the correct device
            if hasattr(batch_queries, "to"):
                batch_queries = batch_queries.to(device)
            elif isinstance(batch_queries, dict):
                batch_queries = {
                    k: v.to(device) if hasattr(v, "to") else v
                    for k, v in batch_queries.items()
                }

            # Generate embeddings for the queries
            query_embeddings = model(**batch_queries)

            # Prepare embeddings result
            embeddings_result = {
                "original_embeddings": query_embeddings.cpu().float().numpy().tolist()
            }

            # Apply hierarchical pooling if requested
            if pooling_type == PoolingType.HIERARCHICAL:
                pooling_result = apply_hierarchical_pooling(
                    query_embeddings, pooling_config
                )
                embeddings_result.update(pooling_result)
            else:
                # No pooling requested
                embeddings_result["pooled_embeddings"] = None
                embeddings_result["pool_factor"] = 1
                embeddings_result["compression_ratio"] = 1.0

        logger.info("Generated embeddings for %s text queries", len(text_queries))

        # Prepare response
        response = {
            "embeddings": embeddings_result,
            "num_texts": len(text_queries),
            "pooling_type": pooling_type.value,
            "model": "vidore/colqwen2-v1.0",
            "input_type": "texts",
            "status": "success",
        }

        return response

    except Exception as e:
        logger.error("Text queries processing failed: %s", str(e))
        raise
