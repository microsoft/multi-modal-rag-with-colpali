#!/usr/bin/env python3
"""
ColQwen2 Model Download Script for Azure ML Pipeline

Downloads ColQwen2 base model and adapter, configures for offline use.
This script is designed to run as a component in an Azure ML pipeline.
"""

import argparse
import json
import logging
import shutil
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def download_colqwen2_model(output_dir: Path):
    """Download ColQwen2 base model and adapter, configure for offline use."""
    logger.info("Downloading ColQwen2 model with base model and adapter...")

    adapter_name = "vidore/colqwen2-v1.0"
    base_model_name = "vidore/colqwen2-base"

    # Import here to avoid issues if packages aren't available
    from colpali_engine.models import ColQwen2Processor
    from huggingface_hub import snapshot_download
    from transformers import AutoModelForVision2Seq

    # Create model directories
    processor_dir = output_dir / "processor"
    model_dir = output_dir / "model"
    base_model_dir = model_dir / "base_model"
    adapter_dir = model_dir / "adapter"

    processor_dir.mkdir(parents=True, exist_ok=True)
    model_dir.mkdir(parents=True, exist_ok=True)
    base_model_dir.mkdir(parents=True, exist_ok=True)
    adapter_dir.mkdir(parents=True, exist_ok=True)

    # Download processor
    logger.info("Downloading processor from %s...", adapter_name)
    processor = ColQwen2Processor.from_pretrained(adapter_name)

    # Handle tuple return from ColQwen2Processor.from_pretrained
    if isinstance(processor, tuple):
        processor = processor[0]

    processor.save_pretrained(str(processor_dir))
    logger.info("Processor saved to %s", processor_dir)

    # Download base model
    logger.info("Downloading base model from %s...", base_model_name)
    base_model = AutoModelForVision2Seq.from_pretrained(
        base_model_name, torch_dtype=None, device_map=None
    )
    base_model.save_pretrained(str(base_model_dir))
    logger.info("Base model saved to %s", base_model_dir)

    # Download adapter
    logger.info("Downloading adapter from %s...", adapter_name)
    snapshot_download(
        repo_id=adapter_name,
        allow_patterns=["adapter_config.json", "adapter_model.safetensors"],
        local_dir=str(adapter_dir),
    )
    logger.info("Adapter saved to %s", adapter_dir)

    # Modify adapter_config.json to point to local base model
    logger.info("Updating adapter_config.json to use local base model...")
    adapter_config_path = adapter_dir / "adapter_config.json"
    with open(adapter_config_path, "r") as f:
        adapter_config = json.load(f)

    with open(adapter_config_path, "w") as f:
        json.dump(adapter_config, f, indent=2)

    # Copy adapter files to model root
    shutil.copy(adapter_dir / "adapter_config.json", model_dir / "adapter_config.json")
    shutil.copy(
        adapter_dir / "adapter_model.safetensors",
        model_dir / "adapter_model.safetensors",
    )
    logger.info("Copied adapter files to model directory")

    # Create model info
    model_info = {
        "name": "colqwen2-v1.0",
        "adapter_name": adapter_name,
        "base_model_name": base_model_name,
        "processor_path": "processor",
        "model_path": "model",
        "architecture": "ColQwen2",
        "base_model": "Qwen/Qwen2-VL-2B-Instruct",
    }

    with open(output_dir / "model_info.json", "w") as f:
        json.dump(model_info, f, indent=2)

    logger.info("Model download and configuration completed successfully!")
    logger.info("Output directory: %s", output_dir)


def main():
    """CLI entry point for the download script."""
    parser = argparse.ArgumentParser(description="Download ColQwen2 model for Azure ML")
    parser.add_argument(
        "--output_dir",
        type=str,
        required=True,
        help="Output directory for the downloaded model",
    )

    args = parser.parse_args()
    output_dir = Path(args.output_dir)

    download_colqwen2_model(output_dir)


if __name__ == "__main__":
    main()
