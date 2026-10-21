# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import base64
import json
import math
import os
import unittest
from io import BytesIO
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw

BASE_URL = os.getenv("COLPALI_TEST_BASE_URL", "").rstrip("/")


@unittest.skipUnless(BASE_URL, "Set COLPALI_TEST_BASE_URL to test a deployed runtime")
class RuntimeIntegrationTests(unittest.TestCase):
    def request(self, path, body=None):
        request = Request(
            f"{BASE_URL}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=180) as response:
                self.assertEqual(response.status, 200)
                return json.load(response)
        except HTTPError as exc:
            self.fail(f"{path} returned HTTP {exc.code}: {exc.read().decode()}")

    def assert_embeddings(self, batch, batch_size, normalized=False):
        self.assertIsInstance(batch, list)
        self.assertEqual(len(batch), batch_size)
        for rows in batch:
            self.assertGreater(len(rows), 0)
            for row in rows:
                self.assertEqual(len(row), 320)
                self.assertTrue(all(math.isfinite(value) for value in row))
                norm = math.sqrt(sum(value * value for value in row))
                self.assertGreater(norm, 0)
                if normalized:
                    self.assertAlmostEqual(norm, 1.0, delta=0.02)

    def test_health_and_text_embeddings(self):
        health = self.request("/health")
        self.assertEqual(health["status"], "healthy")
        self.assertTrue(health["model_loaded"])
        response = self.request(
            "/embeddings",
            {
                "texts": ["Revenue", "What is the total revenue in this document?"],
                "pooling_type": ["none"],
            },
        )
        self.assert_embeddings(response["embeddings"], 2, normalized=True)

    def test_image_embeddings_and_all_pooling_modes(self):
        with Image.new("RGB", (224, 168), "white") as image:
            draw = ImageDraw.Draw(image)
            draw.text((16, 16), "Revenue: 125", fill="black")
            draw.rectangle((16, 48, 180, 100), outline="blue", width=3)
            stream = BytesIO()
            image.save(stream, format="PNG")
        response = self.request(
            "/embeddings",
            {
                "images": [base64.b64encode(stream.getvalue()).decode()],
                "pooling_type": ["none", "hierarchical_pooling", "mean_pooling"],
            },
        )
        self.assert_embeddings(response["embeddings"], 1, normalized=True)
        raw_count = len(response["embeddings"][0])
        for field in (
            "hierarchical_pooled_embeddings",
            "mean_row_pooled_embeddings",
            "mean_column_pooled_embeddings",
        ):
            with self.subTest(field=field):
                self.assert_embeddings(response[field], 1)
                self.assertLessEqual(len(response[field][0]), raw_count)
