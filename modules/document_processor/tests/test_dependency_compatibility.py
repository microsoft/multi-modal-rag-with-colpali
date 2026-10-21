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
from document_processor.document_processor import DocumentProcessor


class DocumentCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.processor = DocumentProcessor(require_service_bus=False)
        self.processor.pdf_image_dpi = 72
        self.client = ColPaliClient(require_endpoint=False)

    async def asyncTearDown(self):
        await self.processor.close()

    async def test_pdf_rendering_and_embedding_payload(self):
        with fitz.open() as document:
            page = document.new_page(width=144, height=72)
            page.insert_text((10, 30), "Compatibility")
            content = document.tobytes()
        pages = self.processor._process_pdf(content, "sample.pdf")
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0].page_number, 1)
        self.assertIn("Compatibility", pages[0].text_content)
        self.assertEqual(pages[0].image_content.size, (144, 72))
        payload = self.client._prepare_payload(pages)
        self.assertEqual(payload.pooling_config, {"pool_factor": 3})
        with Image.open(io.BytesIO(base64.b64decode(payload.images[0]))) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.mode, "RGB")
            self.assertEqual(image.size, (144, 72))

    async def test_transparency_and_dpi_downsampling(self):
        image = Image.new("RGBA", (144, 72), (255, 0, 0, 0))
        image.info["dpi"] = (144, 144)
        pages = self.processor._process_image_object(image, "transparent.png")
        processed = pages[0].image_content
        self.assertEqual(processed.size, (72, 36))
        self.assertEqual(processed.mode, "RGB")
        self.assertEqual(processed.getpixel((0, 0)), (255, 255, 255))

    async def test_png_and_jpeg_ingestion(self):
        for image_format, extension in [("PNG", ".png"), ("JPEG", ".jpg")]:
            with self.subTest(image_format=image_format):
                buffer = io.BytesIO()
                Image.new("RGB", (32, 24), "red").save(buffer, format=image_format)
                pages = self.processor._process_image(
                    buffer.getvalue(), "sample" + extension, extension
                )
                self.assertEqual(len(pages), 1)
                self.assertEqual(pages[0].image_content.size, (32, 24))
                payload = self.client._prepare_payload(pages)
                self.assertEqual(len(payload.images), 1)

    async def test_invalid_image_rejected(self):
        with self.assertLogs(level="ERROR"):
            with self.assertRaises(Image.UnidentifiedImageError):
                self.processor._process_image(b"not an image", "invalid.png", ".png")

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
