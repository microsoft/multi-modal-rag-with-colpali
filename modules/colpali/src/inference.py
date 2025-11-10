"""
ColQwen2 Model Inference Module

Handles model initialization and inference logic for Kubernetes deployment.
Integrates the complete ColQwen2 scoring functionality from k8s_score.py.
"""

import base64
import logging
import os
from enum import Enum
from io import BytesIO
from typing import Any, Dict, List, Optional

import torch
from colpali_engine.compression.token_pooling import HierarchicalTokenPooler
from PIL import Image
from transformers import ColQwen2ForRetrieval, ColQwen2Processor
from transformers.utils.import_utils import is_flash_attn_2_available

# Import models for type hints
from .models import EmbedRequest, EmbedResponse

# Configure logging for Kubernetes environment
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
    MEAN_POOLING = "mean_pooling"


# Global variables for model components
model: Optional[ColQwen2ForRetrieval] = None
processor: Optional[ColQwen2Processor] = None
device: Optional[torch.device] = None
hierarchical_pooler: Optional[HierarchicalTokenPooler] = None

# Constants for model paths - defined in one place
PROCESSOR_SUBDIR = "processor"
MODEL_SUBDIR = "model"


def _get_model_dir_name() -> str:
    """Get the model directory name from MODEL_ID environment variable."""
    model_id = os.getenv("MODEL_ID", "vidore/colqwen2-v1.0-hf")
    # Convert model ID to directory name (e.g., "vidore/colqwen2-v1.0-hf" -> "vidore-colqwen2-v1.0-hf")
    return model_id.replace("/", "-")


def _load_models_from_paths(
    processor_path: str,
    model_path: str,
    torch_dtype,
    device_map="auto",
    device=None,
    for_validation=False,
):
    """
    Load processor and model from given paths with specified configuration.

    Args:
        processor_path: Path to processor directory
        model_path: Path to model directory
        torch_dtype: Torch data type (e.g., torch.bfloat16)
        device_map: Device mapping ("auto", "cpu", etc.)
        device: Target device (required when for_validation=False and device_map is None/cpu)
        for_validation: If True, returns models for validation. If False, sets global variables.

    Returns:
        If for_validation=True: tuple of (processor, model)
        If for_validation=False: None (sets global variables)
    """
    # Load processor
    processor_result = ColQwen2Processor.from_pretrained(
        processor_path, local_files_only=True
    )
    if isinstance(processor_result, tuple):
        loaded_processor = processor_result[0]
    else:
        loaded_processor = processor_result

    # Load model with Flash Attention configuration
    use_flash_attention_2 = is_flash_attn_2_available()
    attn_implementation = "flash_attention_2" if use_flash_attention_2 else "sdpa"

    model_kwargs = {
        "torch_dtype": torch_dtype,
        "local_files_only": True,
        "attn_implementation": attn_implementation,
        "low_cpu_mem_usage": True,
        "use_safetensors": True,
        "trust_remote_code": True,
    }

    if device_map:
        model_kwargs["device_map"] = device_map

    loaded_model = ColQwen2ForRetrieval.from_pretrained(model_path, **model_kwargs)

    if for_validation:
        return loaded_processor, loaded_model
    else:
        # Set global variables for production use
        global model, processor, hierarchical_pooler
        processor = loaded_processor
        model = loaded_model

        # Move model to device if not using device_map
        if (not device_map or device_map == "cpu") and device is not None:
            model = model.to(device)

        model.eval()
        hierarchical_pooler = HierarchicalTokenPooler()


def _can_load_model(processor_path: str, model_path: str) -> bool:
    """
    Check if model can be loaded successfully from the given paths.
    Returns True if both processor and model can be loaded without errors.
    Performs full model loading to ensure integrity.
    """
    try:
        # Check if directories exist and have required files
        if not os.path.exists(processor_path) or not os.path.exists(model_path):
            logger.info(
                "Model paths don't exist: processor=%s, model=%s",
                os.path.exists(processor_path),
                os.path.exists(model_path),
            )
            return False

        # Check for essential config files
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

        # Use shared loading logic for validation
        test_processor, test_model = _load_models_from_paths(
            processor_path,
            model_path,
            torch_dtype=torch.bfloat16,
            device_map="cpu",  # Always use CPU for testing to avoid GPU memory issues
            for_validation=True,
        )

        logger.info("Processor loaded successfully")
        logger.info("Model loaded successfully")

        # Clean up test model from memory immediately
        del test_model, test_processor
        import gc

        gc.collect()

        logger.info(
            "Full model loading test successful - model is complete and functional"
        )
        return True

    except Exception as e:
        logger.info("Full model loading test failed: %s - will re-download", str(e))
        return False


def _download_model_if_needed(output_dir_path: str):
    """Download ColQwen2 model if it doesn't exist locally."""
    import time
    from pathlib import Path

    output_dir = Path(output_dir_path)
    start_time = time.time()

    model_name = os.getenv("MODEL_ID", "vidore/colqwen2-v1.0-hf")
    logger.info("=" * 80)
    logger.info("DOWNLOAD MODE: Starting model download")
    logger.info("=" * 80)
    logger.info("Model ID: %s", model_name)
    logger.info("Output directory: %s", output_dir_path)
    logger.info("HF_HOME cache: %s", os.getenv("HF_HOME", "default"))

    # Create model directories using centralized path logic
    model_dir_name = _get_model_dir_name()
    model_base_dir = output_dir / model_dir_name
    processor_dir = model_base_dir / PROCESSOR_SUBDIR
    model_dir = model_base_dir / MODEL_SUBDIR

    logger.info("Creating directory structure:")
    logger.info("  Base dir: %s", model_base_dir)
    logger.info("  Processor dir: %s", processor_dir)
    logger.info("  Model dir: %s", model_dir)

    processor_dir.mkdir(parents=True, exist_ok=True)
    model_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Directories created successfully")

    # Check if model already exists and can be loaded successfully
    if _can_load_model(str(processor_dir), str(model_dir)):
        logger.info("=" * 80)
        logger.info("MODEL ALREADY EXISTS AND LOADABLE - SKIPPING DOWNLOAD")
        logger.info("=" * 80)
        logger.info("Processor path: %s", processor_dir)
        logger.info("Model path: %s", model_dir)
        logger.info("Total time: 0.00 minutes (cached)")
        logger.info("Model location: %s", model_base_dir)
        logger.info("=" * 80)
        return

    # Clean up any partial downloads to prevent disk space issues
    # Only clean up if directories exist but model can't be loaded
    if processor_dir.exists() or model_dir.exists():
        if not _can_load_model(str(processor_dir), str(model_dir)):
            logger.info("Cleaning up partial/corrupted downloads...")
            import shutil

            if processor_dir.exists():
                shutil.rmtree(processor_dir)
                logger.info("Cleaned up processor directory")
            if model_dir.exists():
                shutil.rmtree(model_dir)
                logger.info("Cleaned up model directory")
            # Recreate directories
            processor_dir.mkdir(parents=True, exist_ok=True)
            model_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Download and save processor
        logger.info("-" * 80)
        logger.info("STEP 1/2: Downloading processor...")
        logger.info("-" * 80)
        processor = ColQwen2Processor.from_pretrained(model_name)
        if isinstance(processor, tuple):
            processor = processor[0]
            logger.info("Processor returned as tuple, using first element")

        logger.info("Saving processor to: %s", processor_dir)
        processor.save_pretrained(str(processor_dir))
        logger.info("Processor downloaded and saved successfully")

        # Download and save model
        logger.info("-" * 80)
        logger.info("STEP 2/2: Downloading model (this may take several minutes)...")
        logger.info("-" * 80)
        use_flash_attention_2 = is_flash_attn_2_available()
        attn_implementation = "flash_attention_2" if use_flash_attention_2 else "sdpa"
        logger.info("Using attention implementation: %s", attn_implementation)

        model = ColQwen2ForRetrieval.from_pretrained(
            model_name,
            torch_dtype=torch.bfloat16,
            device_map="cpu",
            attn_implementation=attn_implementation,
            low_cpu_mem_usage=True,
            use_safetensors=True,
            trust_remote_code=True,
        )
        logger.info("Saving model to: %s", model_dir)
        model.save_pretrained(str(model_dir))
        logger.info("Model downloaded and saved successfully")

    except Exception as e:
        logger.error("Download failed: %s", str(e))
        # Clean up partial downloads on failure to prevent disk space issues
        logger.info("Cleaning up partial downloads due to failure...")
        if processor_dir.exists():
            import shutil

            shutil.rmtree(processor_dir)
            logger.info("Cleaned up partial processor download")
        if model_dir.exists():
            import shutil

            shutil.rmtree(model_dir)
            logger.info("Cleaned up partial model download")
        raise

    total_time = time.time() - start_time
    logger.info("=" * 80)
    logger.info("DOWNLOAD COMPLETE")
    logger.info("=" * 80)
    logger.info("Total time: %.2f minutes", total_time / 60)
    logger.info("Model saved to: %s", model_base_dir)
    logger.info("=" * 80)


def _load_model_from_disk(
    processor_path: str, model_model_path: str, torch_dtype, device
):
    """Helper function to load model from disk using shared loading logic."""
    logger.info("Loading processor from: %s", processor_path)
    logger.info("Loading ColQwen2 merged model from: %s", model_model_path)

    # Determine device mapping
    device_map = "auto" if device.type == "cuda" else None

    # Use shared loading logic for production
    _load_models_from_paths(
        processor_path,
        model_model_path,
        torch_dtype=torch_dtype,
        device_map=device_map,
        device=device,
        for_validation=False,  # Sets global variables
    )

    logger.info("Model loaded and moved to device: %s", device)
    logger.info("Hierarchical pooler initialized")


def init_model():
    """
    Initialize the ColQwen2 model and processor for Kubernetes deployment.
    Adapted from the original Azure ML version to work with Kubernetes volume mounts.
    """
    global model, processor, device, hierarchical_pooler

    logger.info("=" * 80)
    logger.info("INFERENCE MODE: Initializing ColQwen2 model")
    logger.info("=" * 80)

    # Set device
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    logger.info("Device: %s", device)

    # Get model paths using centralized logic
    model_directory_path = os.getenv("MODEL_DIRECTORY_PATH", "/model-directory")
    model_dir_name = _get_model_dir_name()

    # Construct paths
    model_base_path = os.path.join(model_directory_path, model_dir_name)
    processor_path = os.path.join(model_base_path, PROCESSOR_SUBDIR)
    model_model_path = os.path.join(model_base_path, MODEL_SUBDIR)

    logger.info("Model paths:")
    logger.info("  MODEL_DIRECTORY_PATH env: %s", model_directory_path)
    logger.info("  MODEL_ID env: %s", os.getenv("MODEL_ID", "vidore/colqwen2-v1.0-hf"))
    logger.info("  Computed model dir name: %s", model_dir_name)
    logger.info("  Base: %s", model_base_path)
    logger.info("  Processor: %s", processor_path)
    logger.info("  Model: %s", model_model_path)

    # Check if directories exist
    logger.info("-" * 80)
    logger.info("Checking directory structure...")
    logger.info("  Model base directory exists: %s", os.path.exists(model_base_path))
    if os.path.exists(model_base_path):
        logger.info("  Contents: %s", os.listdir(model_base_path))
    logger.info("  Processor directory exists: %s", os.path.exists(processor_path))
    if os.path.exists(processor_path):
        proc_files = os.listdir(processor_path)
        logger.info("  Processor files (%d total): %s", len(proc_files), proc_files)
    logger.info("  Model directory exists: %s", os.path.exists(model_model_path))
    if os.path.exists(model_model_path):
        model_files = os.listdir(model_model_path)
        logger.info("  Model files (%d total): %s", len(model_files), model_files)
    logger.info("-" * 80)

    torch_dtype = torch.bfloat16 if torch.cuda.is_available() else torch.float32
    logger.info("Using torch dtype: %s", torch_dtype)

    # Load pre-downloaded model
    logger.info("Loading pre-downloaded model from InitContainer...")

    try:
        _load_model_from_disk(processor_path, model_model_path, torch_dtype, device)
        logger.info("=" * 80)
        logger.info("ColQwen2 model initialization completed successfully")
        logger.info("=" * 80)

    except Exception as e:
        logger.error("=" * 80)
        logger.error("FAILED to load pre-downloaded model")
        logger.error("=" * 80)
        logger.error("Error: %s", str(e))
        logger.error("Model base path: %s", model_base_path)
        logger.error("Processor path: %s", processor_path)
        logger.error("Model path: %s", model_model_path)

        # Provide debugging information
        logger.error("-" * 80)
        logger.error("Directory diagnostics:")

        if os.path.exists(model_directory_path):
            logger.error("MODEL_DIRECTORY_PATH exists: %s", model_directory_path)
            base_contents = os.listdir(model_directory_path)
            logger.error("  Contents (%d items): %s", len(base_contents), base_contents)
        else:
            logger.error(
                "MODEL_DIRECTORY_PATH does not exist: %s", model_directory_path
            )

        if os.path.exists(model_base_path):
            logger.error("Model base directory exists: %s", model_base_path)
            base_dir_contents = os.listdir(model_base_path)
            logger.error(
                "  Contents (%d items): %s", len(base_dir_contents), base_dir_contents
            )
        else:
            logger.error("Model base directory does not exist: %s", model_base_path)

        if os.path.exists(processor_path):
            logger.error("Processor directory exists: %s", processor_path)
            proc_files = os.listdir(processor_path)
            logger.error("  Files (%d total): %s", len(proc_files), proc_files)
        else:
            logger.error("Processor directory does not exist: %s", processor_path)

        if os.path.exists(model_model_path):
            logger.error("Model directory exists: %s", model_model_path)
            model_files = os.listdir(model_model_path)
            logger.error("  Files (%d total): %s", len(model_files), model_files)
        else:
            logger.error("Model directory does not exist: %s", model_model_path)

        logger.error("=" * 80)

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


def apply_hierarchical_pooling(
    embeddings: torch.Tensor, pooling_config: Optional[Dict[str, Any]] = None
) -> torch.Tensor:
    """
    Apply hierarchical token pooling to embeddings using ColPali's implementation.
    """
    try:
        logger.info("Applying hierarchical token pooling...")

        # Get original shape
        original_shape = embeddings.shape
        logger.info("Original embeddings shape: %s", original_shape)

        # Default pooling configuration
        pool_factor = 2
        if pooling_config and "pool_factor" in pooling_config:
            pool_factor = pooling_config["pool_factor"]

        # Apply pooling using the hierarchical pooler
        pooled_embeddings = hierarchical_pooler.forward(
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


def get_patches(image_size, model_processor, model):
    """Get the number of patches for x and y dimensions."""

    # This code is taken from the example at: https://qdrant.tech/documentation/advanced-tutorials/pdf-retrieval-at-scale/
    return model_processor.get_n_patches(
        image_size,
        patch_size=model.patch_size,
        spatial_merge_size=model.spatial_merge_size,
    )


def generate_embeddings(request: EmbedRequest) -> Dict[str, Any]:
    """
    Main inference function that processes EmbedRequest models.
    This is the main entry point called by the FastAPI wrapper.
    """
    try:
        # Check if model is initialized
        if model is None or processor is None:
            raise RuntimeError("Model not initialized. Call init_model() first.")

        logger.info("Processing inference request")

        # Extract pooling configuration
        pooling_types = []
        for pooling_str in request.pooling_type:
            try:
                pooling_types.append(PoolingType(pooling_str))
            except ValueError:
                logger.warning(f"Invalid pooling type '{pooling_str}', skipping")

        if not pooling_types:
            logger.warning(
                "No valid pooling types specified, defaulting to 'hierarchical'"
            )
            pooling_types = [PoolingType.HIERARCHICAL]

        pooling_config = request.pooling_config

        # Determine request type and process accordingly
        if request.images:
            return process_images_from_request(request, pooling_types, pooling_config)
        elif request.texts:
            return process_texts_from_request(request, pooling_types, pooling_config)
        else:
            raise ValueError("Request must contain either 'images' or 'texts' field")

    except Exception as e:
        logger.error("Inference failed: %s", str(e))
        error_response = {"error": str(e), "status": "error"}
        return error_response


def process_images_from_request(
    request: EmbedRequest,
    pooling_types: List[PoolingType],
    pooling_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Process image embedding request with multiple pooling types.
    """
    try:
        # Extract image data from request
        image_data_list = request.images
        if not image_data_list:
            raise ValueError("Request must contain 'images' field with image content")

        if not isinstance(image_data_list, list):
            raise ValueError("'images' field must be a list")

        if len(image_data_list) == 0:
            raise ValueError("'images' list cannot be empty")

        logger.info(
            "Processing %s images with pooling types: %s",
            len(image_data_list),
            [pt.value for pt in pooling_types],
        )

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

        # Process images ONCE using ColQwen2Processor
        logger.info("Processing images with ColQwen2...")

        with torch.no_grad():
            # Process images using ColQwen2Processor
            batch_images = processor.process_images(images)

            # Move tensors to the correct device
            if hasattr(batch_images, "to"):
                batch_images = batch_images.to(device)
            elif isinstance(batch_images, dict):
                batch_images = {
                    k: v.to(device) if hasattr(v, "to") else v
                    for k, v in batch_images.items()
                }

            # Generate embeddings ONCE
            embeddings = model(**batch_images)

        logger.info("Base embeddings generated, applying requested poolings...")

        # Initialize response structure
        response = {}

        # Apply different pooling types to the SAME embeddings
        # This code is taken from the example at: https://qdrant.tech/documentation/advanced-tutorials/pdf-retrieval-at-scale/
        for pooling_type in pooling_types:
            if pooling_type == PoolingType.MEAN_POOLING:
                # Apply mean pooling to existing embeddings
                logger.info("Applying mean pooling...")

                # Apply mean pooling logic directly
                pooled_by_rows_batch = []
                pooled_by_columns_batch = []
                original_embeddings = embeddings.cpu().float().numpy().tolist()

                for i, (image_embedding, tokenized_image, image) in enumerate(
                    zip(embeddings, batch_images.input_ids, images)
                ):
                    x_patches, y_patches = get_patches(image.size, processor, model)

                    image_tokens_mask = tokenized_image == processor.image_token_id

                    # Get embedding dimension from the shape of the tensor
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

                    # Adding back prefix and postfix special tokens
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
                            (prefix_tokens, pooled_by_columns, postfix_tokens), dim=0
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

                logger.info("Mean pooling completed")

            elif pooling_type == PoolingType.HIERARCHICAL:
                # Apply hierarchical pooling to existing embeddings
                logger.info("Applying hierarchical pooling...")

                pooled_embeddings = apply_hierarchical_pooling(
                    embeddings, pooling_config
                )
                final_embeddings = pooled_embeddings.cpu().float().numpy().tolist()

                response["hierarchical_pooled_embeddings"] = {
                    "hierarchical": final_embeddings
                }

                logger.info("Hierarchical pooling completed")

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


def process_texts_from_request(
    request: EmbedRequest,
    pooling_types: List[PoolingType],
    pooling_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Process text queries embedding request with multiple pooling types.
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

        logger.info(
            "Processing %s text queries with pooling types: %s",
            len(text_queries),
            [pt.value for pt in pooling_types],
        )

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

        # Initialize response structure
        response = {}

        # Process different pooling types
        for pooling_type in pooling_types:
            if pooling_type == PoolingType.HIERARCHICAL:
                # Apply hierarchical pooling
                logger.info("Applying hierarchical pooling to text queries...")

                with torch.no_grad():
                    # Process text queries using ColQwen2Processor
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

                    pooled_embeddings = apply_hierarchical_pooling(
                        query_embeddings, pooling_config
                    )
                    final_embeddings = pooled_embeddings.cpu().float().numpy().tolist()

                response["hierarchical_pooled_embeddings"] = {
                    "hierarchical": final_embeddings
                }

                logger.info("Hierarchical pooling completed for text queries")

            elif pooling_type == PoolingType.NONE:
                # No pooling - use original embeddings
                logger.info("Processing text queries without pooling...")

                with torch.no_grad():
                    # Process text queries using ColQwen2Processor
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
                    final_embeddings = query_embeddings.cpu().float().numpy().tolist()

                response["embeddings"] = final_embeddings

                logger.info("Original embeddings generated for text queries")

            # Note: Mean pooling for text queries is typically not applicable
            # as text embeddings don't have the same spatial structure as images
            elif pooling_type == PoolingType.MEAN_POOLING:
                logger.warning(
                    "Mean pooling is not applicable for text queries, skipping"
                )

        logger.info("Generated embeddings for %s text queries", len(text_queries))
        return response

    except Exception as e:
        logger.error("Text queries processing failed: %s", str(e))
        raise
