# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from colpali_inference.download_model import (
    MARKER_FILENAME,
    download_model_from_hf,
)


class ModelDownloadTests(unittest.TestCase):
    def test_quantized_rollout_preserves_metadata_and_original_cache(self):
        original_id = "TomoroAI/tomoro-colqwen3-embed-4b"
        quantized_id = "TomoroAI/tomoro-ai-colqwen3-embed-4b-awq"
        # Published checkpoint metadata: the name is not the packing format.
        quantization = {
            "quant_method": "auto-round",
            "packing_format": "auto_round:auto_gptq",
            "bits": 4,
            "group_size": 128,
            "sym": True,
            "block_name_to_quantize": "vlm.model.language_model.layers",
        }
        config = {
            "architectures": ["ColQwen3"],
            "embed_dim": 320,
            "text_config": {"tie_word_embeddings": True},
            "quantization_config": quantization,
        }

        def download_snapshot(*, repo_id, repo_type, local_dir):
            self.assertEqual(repo_id, quantized_id)
            self.assertEqual(repo_type, "model")
            target = Path(local_dir)
            (target / "config.json").write_text(json.dumps(config), encoding="utf-8")
            (target / "quantization_config.json").write_text(
                json.dumps(quantization), encoding="utf-8"
            )
            (target / "model.safetensors").write_bytes(b"quantized snapshot fixture")

        with tempfile.TemporaryDirectory() as root:
            original = Path(root) / original_id.split("/")[-1]
            original.mkdir()
            (original / MARKER_FILENAME).write_text(original_id, encoding="utf-8")
            (original / "model.safetensors").write_bytes(b"original snapshot fixture")
            with (
                patch.dict(os.environ, {"MODEL_DIRECTORY_PATH": root}),
                patch(
                    "colpali_inference.download_model.snapshot_download",
                    side_effect=download_snapshot,
                ) as download,
            ):
                os.environ.pop("MODEL_ID", None)
                target = Path(download_model_from_hf())
                self.assertEqual(target, Path(root) / quantized_id.split("/")[-1])
                self.assertEqual(
                    (target / MARKER_FILENAME).read_text(encoding="utf-8"),
                    quantized_id,
                )
                self.assertEqual(
                    json.loads((target / "config.json").read_text(encoding="utf-8")),
                    {**config, "tie_word_embeddings": True},
                )
                self.assertEqual(
                    json.loads(
                        (target / "quantization_config.json").read_text(
                            encoding="utf-8"
                        )
                    ),
                    quantization,
                )
                self.assertEqual(Path(download_model_from_hf()), target)
                download.assert_called_once()
                self.assertEqual(
                    (target / "model.safetensors").read_bytes(),
                    b"quantized snapshot fixture",
                )
                self.assertEqual(
                    (original / "model.safetensors").read_bytes(),
                    b"original snapshot fixture",
                )
                self.assertEqual(
                    (original / MARKER_FILENAME).read_text(encoding="utf-8"),
                    original_id,
                )


if __name__ == "__main__":
    unittest.main()
