# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from transformers import Qwen2VLImageProcessorFast

from colpali_inference.inference import ColPaliInference


@unittest.skipUnless(shutil.which("bash"), "Docker's shell launch check requires bash")
class DeploymentContractTests(unittest.TestCase):
    def test_vllm_launch_and_shim_preprocessing_keep_the_same_image_tokens(self):
        dockerfile = (
            Path(__file__).resolve().parents[1] / "Dockerfile.vllm"
        ).read_text()
        command = json.loads(dockerfile.split("ENTRYPOINT ", 1)[1].replace("\\\n", ""))[
            2
        ]
        # Docker expands PATH during the build; this probe needs the host's PATH.
        defaults = dict(
            line[4:].split("=", 1)
            for line in dockerfile.splitlines()
            if line.startswith("ENV ") and not line.startswith("ENV PATH=")
        )
        capture = 'vllm() { printf "%s\\n" "$@"; }; ' + command.replace(
            "exec vllm serve", "vllm serve"
        )
        for overrides in (
            {},
            {"COLPALI_MAX_IMAGE_PIXELS": "1048576", "VLLM_CPU_OFFLOAD_GB": "0"},
            {"COLPALI_MAX_IMAGE_PIXELS": "1048576", "VLLM_CPU_OFFLOAD_GB": "0.3"},
        ):
            with self.subTest(overrides=overrides):
                env = {**os.environ, **defaults, **overrides}
                args = subprocess.check_output(
                    [shutil.which("bash"), "-c", capture],
                    env=env,
                    text=True,
                    timeout=30,
                ).splitlines()
                settings = json.loads(args[args.index("--mm-processor-kwargs") + 1])
                self.assertEqual(
                    float(args[args.index("--cpu-offload-gb") + 1]),
                    float(env["VLLM_CPU_OFFLOAD_GB"]),
                )
                shim = ColPaliInference(vllm_client=SimpleNamespace())
                shim.processor = SimpleNamespace(
                    image_processor=Qwen2VLImageProcessorFast(
                        patch_size=16,
                        merge_size=2,
                        min_pixels=65536,
                        max_pixels=1310720,
                    )
                )
                with patch.dict(os.environ, env):
                    shim._configure_image_cap()
                standard = Qwen2VLImageProcessorFast(
                    patch_size=16,
                    merge_size=2,
                    min_pixels=65536,
                    max_pixels=1310720,
                )
                for size in ((1240, 1754), (2400, 2400)):
                    with Image.new("RGB", size) as image:
                        actual = standard(images=image, return_tensors="pt", **settings)
                        expected = shim.processor.image_processor(
                            images=image, return_tensors="pt"
                        )
                    self.assertEqual(
                        actual["image_grid_thw"].tolist(),
                        expected["image_grid_thw"].tolist(),
                    )
                    tokens = int(actual["image_grid_thw"][0].prod()) // 4
                    if not overrides and size == (1240, 1754):
                        self.assertEqual(tokens, 2145)
                        self.assertLess(
                            tokens + 32, int(args[args.index("--max-model-len") + 1])
                        )
