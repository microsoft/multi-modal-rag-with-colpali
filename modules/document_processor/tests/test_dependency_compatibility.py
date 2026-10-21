# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import base64
import io
import unittest

import fitz
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from msal.oauth2cli.assertion import JwtAssertionCreator
from PIL import Image

from document_processor.colpali_client import ColPaliClient
from document_processor.page_processing import PageRenderer


class DocumentCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.renderer = PageRenderer(dpi=72, max_pixels=75000000, crop=False)
        self.client = ColPaliClient(require_endpoint=False)
        self.client.embedding_dpi = 72

    def test_pdf_rendering_and_embedding_payload(self):
        with fitz.open() as document:
            page = document.new_page(width=144, height=72)
            page.insert_text((10, 30), "Compatibility")
            content = document.tobytes()
        pages = list(self.renderer.iter_pdf(content, "sample.pdf", set()))
        for page in pages:
            self.addCleanup(page.image_content.close)
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0].page_number, 1)
        self.assertIn("Compatibility", pages[0].text_content)
        self.assertEqual(pages[0].image_content.size, (144, 72))
        payload = self.client._prepare_payload(pages)
        self.assertEqual(payload["pooling_config"], {"pool_factor": 3})
        with Image.open(io.BytesIO(base64.b64decode(payload["images"][0]))) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.mode, "RGB")
            self.assertEqual(image.size, (144, 72))

    def test_transparency_and_dpi_downsampling(self):
        buffer = io.BytesIO()
        with Image.new("RGBA", (144, 72), (255, 0, 0, 0)) as image:
            image.save(buffer, format="PNG", dpi=(144, 144))
        page = self.renderer.image_page(buffer.getvalue(), "transparent.png")
        processed = page.image_content
        self.addCleanup(processed.close)
        self.assertEqual(processed.size, (144, 72))
        self.assertEqual(processed.mode, "RGB")
        self.assertEqual(processed.getpixel((0, 0)), (255, 255, 255))
        payload = self.client._prepare_payload([page])
        with Image.open(io.BytesIO(base64.b64decode(payload["images"][0]))) as image:
            self.assertEqual(image.size, (72, 36))
            self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))
        self.assertEqual(processed.size, (144, 72))

    def test_png_and_jpeg_ingestion(self):
        for image_format, extension in [("PNG", ".png"), ("JPEG", ".jpg")]:
            with self.subTest(image_format=image_format):
                buffer = io.BytesIO()
                with Image.new("RGB", (32, 24), "red") as image:
                    image.save(buffer, format=image_format)
                page = self.renderer.image_page(buffer.getvalue(), "sample" + extension)
                self.addCleanup(page.image_content.close)
                self.assertEqual(page.image_content.size, (32, 24))
                payload = self.client._prepare_payload([page])
                with Image.open(
                    io.BytesIO(base64.b64decode(payload["images"][0]))
                ) as image:
                    self.assertEqual(image.size, (32, 24))
                    self.assertGreater(image.getpixel((0, 0))[0], 245)

    def test_invalid_image_rejected(self):
        with self.assertRaises(Image.UnidentifiedImageError):
            self.renderer.image_page(b"not an image", "invalid.png")

    def test_msal_assertion_signing_with_cryptography(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        token = JwtAssertionCreator(pem, algorithm="RS256").create_normal_assertion(
            "https://example.com/token", "client-id"
        )
        claims = jwt.decode(
            token,
            key.public_key(),
            algorithms=["RS256"],
            audience="https://example.com/token",
        )
        self.assertEqual(claims["sub"], "client-id")


if __name__ == "__main__":
    unittest.main()
