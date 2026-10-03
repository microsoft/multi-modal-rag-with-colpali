# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""
Download model files from HuggingFace Hub into the per-replica PVC mount.

The init container runs ``python -m colpali_inference.app download``,
which calls :func:`download_model_from_hf`. The downloaded snapshot lives
at ``${MODEL_DIRECTORY_PATH}/<basename(MODEL_ID)>`` — the same path the
vLLM sidecar serves from and the shim processor loads from.

Required env vars:
    MODEL_DIRECTORY_PATH - PVC mount point (e.g. /mnt/models/offline)
    MODEL_ID             - HuggingFace model ID (e.g. TomoroAI/tomoro-colqwen3-embed-4b)
"""

import json
import logging
import os
import shutil

from huggingface_hub import snapshot_download

logger = logging.getLogger(__name__)

MARKER_FILENAME = ".download_complete"


def _patch_config_for_vllm(target_dir: str) -> None:
    """Ensure top-level ``tie_word_embeddings: true`` in ``config.json``.

    ColQwen3 ships ``tie_word_embeddings`` only inside the nested
    ``text_config``. Some vLLM versions do not propagate that to the top
    level when loading the multimodal model, so loading fails with::

        ValueError: Following weights were not initialized from checkpoint:
                    {'language_model.lm_head.weight'}

    Setting the top-level key tells vLLM to share ``lm_head`` weights with
    the input embeddings, matching the model's actual tied-weight design.
    Idempotent — safe to run on already-patched snapshots.
    """
    config_path = os.path.join(target_dir, "config.json")
    if not os.path.isfile(config_path):
        logger.warning("config.json not found at %s — skipping vLLM patch", config_path)
        return

    try:
        with open(config_path, "r") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.error("Failed to read config.json for patching: %s", e)
        return

    if cfg.get("tie_word_embeddings") is True:
        logger.info("config.json already has top-level tie_word_embeddings=true")
        return

    cfg["tie_word_embeddings"] = True
    tmp_path = config_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp_path, config_path)
    logger.info(
        "Patched %s with top-level tie_word_embeddings=true (vLLM lm_head fix)",
        config_path,
    )


def _model_name_from_id(model_id: str) -> str:
    """Extract the local directory name from a HuggingFace model ID.

    Example: ``TomoroAI/tomoro-colqwen3-embed-4b`` -> ``tomoro-colqwen3-embed-4b``.
    """
    return model_id.split("/")[-1]


def _get_target_dir(model_directory_path: str, model_id: str) -> str:
    """Compute the local target directory for a given model."""
    return os.path.join(model_directory_path, _model_name_from_id(model_id))


def _needs_download(target_dir: str, model_id: str) -> bool:
    """Return True if the snapshot is missing or doesn't match ``MODEL_ID``."""
    marker_path = os.path.join(target_dir, MARKER_FILENAME)
    if not os.path.isfile(marker_path):
        return True
    try:
        with open(marker_path, "r") as f:
            stored_model_id = f.read().strip()
        if stored_model_id != model_id:
            logger.info(
                "Model ID changed: marker has '%s', current is '%s'",
                stored_model_id,
                model_id,
            )
            return True
    except OSError:
        return True
    return False


def _write_marker(target_dir: str, model_id: str) -> None:
    """Write the completion marker with the current model ID."""
    marker_path = os.path.join(target_dir, MARKER_FILENAME)
    with open(marker_path, "w") as f:
        f.write(model_id)


def download_model_from_hf() -> str:
    """Download model snapshot from HuggingFace Hub into the shared PVC.

    Returns the local target directory.

    Raises:
        EnvironmentError: If ``MODEL_DIRECTORY_PATH`` is not set.
    """
    model_directory_path = os.environ.get("MODEL_DIRECTORY_PATH")
    model_id = os.environ.get("MODEL_ID", "TomoroAI/tomoro-colqwen3-embed-4b")

    if not model_directory_path:
        raise EnvironmentError(
            "MODEL_DIRECTORY_PATH must be set (PVC mount point for model weights)"
        )

    target_dir = _get_target_dir(model_directory_path, model_id)

    logger.info("Model ID:      %s", model_id)
    logger.info("Target dir:    %s", target_dir)

    if not _needs_download(target_dir, model_id):
        logger.info("Model already present at %s — skipping download", target_dir)
        # Always apply vLLM config patch — idempotent, and ensures existing
        # PVCs get the fix without forcing a re-download.
        _patch_config_for_vllm(target_dir)
        return target_dir

    if os.path.exists(target_dir):
        logger.info("Removing stale model directory: %s", target_dir)
        shutil.rmtree(target_dir)
    os.makedirs(target_dir, exist_ok=True)

    logger.info("Downloading %s from HuggingFace Hub...", model_id)
    snapshot_download(
        repo_id=model_id,
        repo_type="model",
        local_dir=target_dir,
    )
    logger.info("HuggingFace download complete")

    _write_marker(target_dir, model_id)
    logger.info("Marker written to %s", target_dir)

    # Apply vLLM config patch before vLLM tries to load the snapshot.
    _patch_config_for_vllm(target_dir)

    return target_dir
