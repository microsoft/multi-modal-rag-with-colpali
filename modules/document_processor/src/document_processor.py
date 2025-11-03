"""
Document processor for handling PDF files and splitting them into page chunks.
"""

import io
import logging
import os
from typing import Any, Dict, List

import fitz  # PyMuPDF
from PIL import Image


class DocumentProcessor:
    """Processes documents and splits them into chunks for embedding generation."""

    def __init__(self):
        self.supported_formats = {".pdf"}  # Only PDF support for now
        self.pdf_image_dpi = int(
            os.getenv("COLPALI_PDF_IMAGE_DPI", "200")
        )  # Increased for better quality

        logging.info(f"DocumentProcessor initialized - DPI: {self.pdf_image_dpi}")

    def process_document(
        self, content: bytes, filename: str, file_type: str
    ) -> List[Dict[str, Any]]:
        """
        Process a document and split it into chunks/pages.

        Args:
            content: Raw document content as bytes
            filename: Name of the file
            file_type: File extension (e.g., '.pdf', '.docx')

        Returns:
            List of document chunks with metadata
        """
        logging.info(f"Processing {file_type} document: {filename}")

        if file_type not in self.supported_formats:
            raise ValueError(f"Unsupported file format: {file_type}")

        chunks = []

        try:
            if file_type == ".pdf":
                chunks = self._process_pdf(content, filename)
            else:
                raise ValueError(f"Only PDF files are supported, got: {file_type}")

        except Exception as e:
            logging.error(f"Error processing document {filename}: {str(e)}")
            raise

        logging.info(f"Successfully processed {filename} into {len(chunks)} chunks")
        return chunks

    def _process_pdf(self, content: bytes, filename: str) -> List[Dict[str, Any]]:
        """Process PDF document into page chunks."""
        chunks = []

        # Use PyMuPDF for better image extraction and text handling
        pdf_document = fitz.open(stream=content, filetype="pdf")

        # Process all pages - no artificial limits
        total_pages = len(pdf_document)
        logging.info(f"Processing PDF with {total_pages} pages")

        for page_num in range(total_pages):
            page = pdf_document.load_page(page_num)

            # Extract images from page
            page_images = []
            image_list = page.get_images()

            for img_index, img in enumerate(image_list):
                try:
                    # Get image data
                    xref = img[0]
                    base_image = pdf_document.extract_image(xref)
                    image_bytes = base_image["image"]

                    # Convert to PIL Image for processing
                    image = Image.open(io.BytesIO(image_bytes))
                    page_images.append({"image": image, "index": img_index})
                except Exception as e:
                    logging.warning(
                        f"Could not extract image {img_index} from page {page_num}: {e}"
                    )

            # Render page as image for ColPali processing using configured DPI
            zoom_factor = self.pdf_image_dpi / 72.0  # 72 DPI is default
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom_factor, zoom_factor))
            page_image = Image.open(io.BytesIO(pix.tobytes("png")))

            chunk = {
                "page_number": page_num + 1,
                "page_image": page_image,  # Main page image for ColPali - contains all visual info including text
                "extracted_images": page_images,  # Additional images found on page
                "source_file": filename,
                "chunk_type": "pdf_page",
            }

            chunks.append(chunk)

        pdf_document.close()
        return chunks
