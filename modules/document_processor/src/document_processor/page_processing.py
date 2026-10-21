# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Incremental rendering with bounded image lifetimes and markdown extraction."""

import asyncio
import io
import logging
import math
from concurrent.futures import ThreadPoolExecutor

import fitz
import pymupdf4llm
from PIL import Image, ImageChops

from .models import DocumentPage

logger = logging.getLogger(__name__)
# MuPDF is not thread-safe. Serialize its calls, including generator teardown,
# while network operations and image encoding run independently.
PDF_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pdf-render")


async def run_cpu(function, *args, executor=None):
    future = asyncio.get_running_loop().run_in_executor(executor, function, *args)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(future)
            break
        except asyncio.CancelledError:
            cancelled = True
            if future.cancelled():
                raise
        except Exception:
            if cancelled:
                logger.exception("CPU operation failed while cancelling")
                raise asyncio.CancelledError
            raise
    if cancelled:
        # Rendering callers own image cleanup; do not abandon a live bitmap.
        if isinstance(result, Image.Image):
            result.close()
        elif isinstance(result, DocumentPage):
            result.image_content.close()
        raise asyncio.CancelledError
    return result


def crop_whitespace(image: Image.Image, margin: int = 10) -> Image.Image:
    with Image.new("RGB", image.size, "white") as background:
        with ImageChops.difference(image, background) as difference:
            with difference.convert("L") as gray:
                with gray.point(lambda value: 255 if value > 30 else 0) as mask:
                    bounds = mask.getbbox()
    if bounds is None:
        return image
    left, top, right, bottom = bounds
    return image.crop(
        (
            max(0, left - margin),
            max(0, top - margin),
            min(image.width, right + margin),
            min(image.height, bottom + margin),
        )
    )


class PageRenderer:
    def __init__(self, dpi: int, max_pixels: int, crop: bool = True):
        if min(dpi, max_pixels) <= 0:
            raise ValueError("Render DPI and pixel limit must be positive")
        self.dpi, self.max_pixels, self.crop = dpi, max_pixels, crop

    def pdf_info(self, content: bytes) -> tuple[int, dict]:
        with fitz.open(stream=content, filetype="pdf") as document:
            if document.needs_pass:
                raise ValueError("Encrypted PDFs are not supported")
            return len(document), document.metadata or {}

    def iter_pdf(self, content: bytes, filename: str, skip_pages: set[int]):
        with fitz.open(stream=content, filetype="pdf") as document:
            for index in range(len(document)):
                if index + 1 in skip_pages:
                    continue
                page = document[index]
                zoom = self.dpi / 72
                pixels = page.rect.width * page.rect.height * zoom * zoom
                if (
                    math.ceil(page.rect.width * zoom)
                    * math.ceil(page.rect.height * zoom)
                    > self.max_pixels
                ):
                    zoom *= min(1, math.sqrt(self.max_pixels / pixels))
                    width = min(self.max_pixels, max(1, int(page.rect.width * zoom)))
                    height = min(
                        self.max_pixels // width, max(1, int(page.rect.height * zoom))
                    )
                    zoom = min(
                        zoom, width / page.rect.width, height / page.rect.height
                    ) * (1 - 1e-9)
                    logger.warning(
                        "Reducing render resolution for page %s to respect pixel limit",
                        index + 1,
                    )
                pixmap = page.get_pixmap(
                    matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csRGB, alpha=False
                )
                image = Image.frombytes(
                    "RGB", (pixmap.width, pixmap.height), pixmap.samples
                )
                del pixmap
                try:
                    try:
                        chunks = pymupdf4llm.to_markdown(
                            document,
                            pages=[index],
                            page_chunks=True,
                            ignore_images=True,
                        )
                        text = chunks[0]["text"]
                    except Exception:
                        logger.warning(
                            "Markdown extraction failed on page %s; using raw text",
                            index + 1,
                            exc_info=True,
                        )
                        text = page.get_text()
                    # Hashing uses the full page; cropping happens after change detection.
                    output = DocumentPage(
                        filename=filename,
                        page_number=index + 1,
                        image_content=image,
                        text_content=text,
                        effective_dpi=max(1, round(zoom * 72)),
                    )
                except BaseException:
                    image.close()
                    raise
                yield output
                del output, image

    def image_page(self, content: bytes, filename: str) -> DocumentPage:
        with Image.open(io.BytesIO(content)) as original:
            original.load()
            width, height = original.size
            if original.width * original.height > self.max_pixels:
                scale = math.sqrt(self.max_pixels / (original.width * original.height))
                original.thumbnail(
                    (
                        max(1, int(original.width * scale)),
                        max(1, int(original.height * scale)),
                    )
                )
            if original.mode in ("RGBA", "LA") or "transparency" in original.info:
                with original.convert("RGBA") as rgba:
                    image = Image.new("RGB", rgba.size, "white")
                    with rgba.getchannel("A") as alpha:
                        image.paste(rgba, mask=alpha)
            else:
                image = original.convert("RGB")
            dpi = original.info.get("dpi", (self.dpi, self.dpi))
            dpi = dpi[0] if isinstance(dpi, (tuple, list)) else dpi
            if not isinstance(dpi, (int, float)) or not math.isfinite(dpi) or dpi <= 0:
                logger.warning("Invalid image DPI; using configured storage DPI")
                dpi = self.dpi
            effective_dpi = max(
                1, round(dpi * min(original.width / width, original.height / height))
            )
        return DocumentPage(
            filename=filename,
            image_content=image,
            effective_dpi=effective_dpi,
        )
