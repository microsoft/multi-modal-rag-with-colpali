# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""
Document processor for handling PDF files with Service Bus message consumption and full processing pipeline.
Combines PDF processing, Service Bus consumption, ColPali embedding generation, and QDRANT indexing.
"""

import asyncio
import io
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Union

import fitz  # PyMuPDF
from azure.identity.aio import DefaultAzureCredential, ManagedIdentityCredential
from azure.servicebus import (
    AutoLockRenewer,
    ServiceBusMessage,
    ServiceBusReceivedMessage,
)
from azure.servicebus.aio import ServiceBusClient
from azure.storage.blob import ContentSettings
from azure.storage.blob.aio import BlobServiceClient
from PIL import Image
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
)

from .colpali_client import ColPaliClient
from .models import (
    BlobEvent,
    DocumentPage,
    ProcessedPage,
    ProcessingResult,
    ServiceBusEvent,
)
from .qdrant_index import QdrantIndex
from .setup_logging import trace_operation


class DocumentProcessor:
    """
    Complete document processing service with Service Bus consumption and full pipeline.
    Handles PDF processing, Service Bus messages, ColPali embedding generation, and QDRANT indexing.
    """

    def __init__(
        self,
        require_service_bus: bool = True,
        pooling_types: Optional[List] = None,
        delete_existing_pages: bool = True,
        shutdown_event: Optional[asyncio.Event] = None,
    ):
        # PDF and image processing configuration
        self.supported_formats = {
            ".pdf",
            ".jpg",
            ".jpeg",
            ".png",
        }
        self.pdf_image_dpi = int(os.getenv("COLPALI_IMAGE_DPI", "72"))

        # Shutdown event for graceful termination
        self.shutdown_event = shutdown_event or asyncio.Event()

        # Service Bus configuration - REQUIRED for production service (managed identity only)
        self.service_bus_namespace = os.getenv("SERVICE_BUS_NAMESPACE_NAME")
        self.queue_name = os.getenv("SERVICE_BUS_QUEUE_NAME", "document-processing")

        # Storage configuration - REQUIRED for blob access
        self.data_storage_account = os.getenv("DATA_STORAGE_ACCOUNT_NAME")

        # Validate required Service Bus configuration (unless disabled for local testing)
        if require_service_bus and not self.service_bus_namespace:
            raise ValueError(
                "Service Bus configuration missing! SERVICE_BUS_NAMESPACE_NAME must be set"
            )

        # Validate required storage configuration (unless disabled for local testing)
        if require_service_bus and not self.data_storage_account:
            raise ValueError(
                "Storage configuration missing! DATA_STORAGE_ACCOUNT_NAME must be set"
            )

        # Initialize Azure credential
        azure_client_id = os.getenv("AZURE_CLIENT_ID")
        if azure_client_id:
            self.credential = ManagedIdentityCredential(client_id=azure_client_id)
        else:
            self.credential = DefaultAzureCredential()

        # Configuration
        self.delete_existing_pages = delete_existing_pages

        # Initialize processing components (QDRANT and ColPali client)
        self.qdrant_index = QdrantIndex(
            credential=self.credential, require_endpoint=require_service_bus
        )
        self.colpali_client = ColPaliClient(
            require_endpoint=require_service_bus, pooling_types=pooling_types
        )

        # Track initialization state with async lock for thread safety
        self._index_initialized = False
        self._init_lock = asyncio.Lock()

        # Service Bus client (initialized later)
        self.service_bus_client = None

        # Blob storage client for uploading page images (initialized lazily)
        self._blob_service_client = None
        self._blob_client_lock = asyncio.Lock()  # Lock for thread-safe singleton

        # Blob concurrency control - limits parallel blob operations across all documents
        blob_max_concurrent = int(os.getenv("BLOB_MAX_CONCURRENT", "250"))
        self._blob_semaphore = asyncio.Semaphore(blob_max_concurrent)

        logging.debug("DocumentProcessor initialized - DPI: %s", self.pdf_image_dpi)
        logging.debug("Service Bus namespace: %s", self.service_bus_namespace)
        logging.debug("Service Bus queue: %s", self.queue_name)
        logging.debug("Blob max concurrent: %d", blob_max_concurrent)

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit - cleanup resources."""
        await self.close()
        return False

    async def close(self):
        """Close all async resources properly to prevent connection leaks."""
        logging.debug("Closing DocumentProcessor resources...")

        # Close blob service client
        if self._blob_service_client:
            try:
                await self._blob_service_client.close()
                logging.debug("Blob service client closed")
            except Exception as e:
                logging.warning("Error closing blob service client: %s", e)
            finally:
                self._blob_service_client = None

        # Close service bus client
        if self.service_bus_client:
            try:
                await self.service_bus_client.close()
                logging.debug("Service bus client closed")
            except Exception as e:
                logging.warning("Error closing service bus client: %s", e)
            finally:
                self.service_bus_client = None

        # Close credential if it has async close
        if hasattr(self.credential, "close"):
            try:
                await self.credential.close()
                logging.debug("Credential closed")
            except Exception as e:
                logging.warning("Error closing credential: %s", e)

        logging.debug("DocumentProcessor resources closed")

    @trace_operation("process_document")
    def process_document(
        self, content: Union[bytes, Image.Image], filename: str, file_type: str
    ) -> List[DocumentPage]:
        """
        Process a document and split it into chunks/pages.

        Args:
            content: Raw document content as bytes, or PIL Image object for direct image processing
            filename: Name of the file
            file_type: File extension (e.g., '.pdf', '.jpg', '.jpeg', '.png')

        Returns:
            List of document chunks with metadata
        """
        logging.info("Processing %s document: %s", file_type, filename)

        # Handle PIL Image objects directly
        if isinstance(content, Image.Image):
            logging.debug("Processing PIL Image object directly")
            return self._process_image_object(content, filename)

        if file_type not in self.supported_formats:
            raise ValueError(f"Unsupported file format: {file_type}")

        pages = []

        try:
            if file_type == ".pdf":
                pages = self._process_pdf(content, filename)
            elif file_type.lower() in {".jpg", ".jpeg", ".png"}:
                pages = self._process_image(content, filename, file_type)
            else:
                raise ValueError(f"Unsupported file format: {file_type}")

        except Exception as e:
            logging.error("Error processing document %s: %s", filename, str(e))
            raise

        logging.debug("Successfully processed %s into %d pages", filename, len(pages))
        return pages

    @trace_operation("process_pdf")
    def _process_pdf(self, content: bytes, filename: str) -> List[DocumentPage]:
        """Process PDF document into page chunks."""
        pages = []

        # Use PyMuPDF for better image extraction and text handling
        pdf_document = fitz.open(stream=content, filetype="pdf")

        # Extract PDF document metadata
        pdf_metadata = pdf_document.metadata
        logging.debug("PDF metadata: %s", pdf_metadata)

        # Clean and process metadata to ensure JSON serializable values
        clean_metadata = {}
        if pdf_metadata:
            for key, value in pdf_metadata.items():
                if value is not None:
                    # Convert to string and handle encoding issues
                    try:
                        # Ensure the value is JSON serializable
                        if isinstance(value, (str, int, float, bool)):
                            clean_metadata[key.lower().replace(" ", "_")] = value
                        else:
                            clean_metadata[key.lower().replace(" ", "_")] = str(value)
                    except Exception as e:
                        logging.warning("Could not process metadata key %s: %s", key, e)
                        clean_metadata[key.lower().replace(" ", "_")] = str(value)

        # Process all pages - no artificial limits
        total_pages = len(pdf_document)
        logging.debug("Processing PDF with %d pages", total_pages)

        for page_num in range(total_pages):
            page = pdf_document.load_page(page_num)

            # Extract text from page
            text = str(page.get_text())

            # Render page as image for ColPali processing using configured DPI
            zoom_factor = self.pdf_image_dpi / 72.0  # 72 DPI is default
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom_factor, zoom_factor))
            page_image = Image.open(io.BytesIO(pix.tobytes("png")))

            # Create DocumentPage model with single image and metadata
            document_page = DocumentPage(
                filename=filename,
                page_number=page_num + 1,
                text_content=text,
                image_content=page_image,
                metadata=clean_metadata,
            )

            pages.append(document_page)

        pdf_document.close()
        return pages

    @trace_operation("process_image")
    def _process_image(
        self, content: bytes, filename: str, file_type: str
    ) -> List[DocumentPage]:
        """
        Process image file (JPEG/PNG) into a single DocumentPage.

        Args:
            content: Raw image content as bytes
            filename: Name of the image file
            file_type: File extension (e.g., '.jpg', '.jpeg', '.png')

        Returns:
            List containing a single DocumentPage
        """
        logging.debug("Processing %s image: %s", file_type, filename)

        try:
            # Load image from bytes
            image = Image.open(io.BytesIO(content))
            return self._process_image_object(image, filename)

        except Exception as e:
            logging.error("Error processing image %s: %s", filename, str(e))
            raise

    @trace_operation("process_image_object")
    def _process_image_object(
        self, image: Image.Image, filename: str
    ) -> List[DocumentPage]:
        """
        Process a PIL Image object into a single DocumentPage with DPI standardization.

        Args:
            image: PIL Image object
            filename: Name/identifier for the image

        Returns:
            List containing a single DocumentPage
        """
        logging.debug("Processing PIL Image object: %s", filename)

        try:
            # Get original DPI if available
            original_dpi = image.info.get("dpi", (72, 72))
            if isinstance(original_dpi, (int, float)):
                original_dpi = (original_dpi, original_dpi)

            logging.debug("Original image DPI: %s", original_dpi)
            logging.debug("Original image size: %s", image.size)

            # Calculate scaling factor to achieve target DPI
            target_dpi = self.pdf_image_dpi
            scale_factor = target_dpi / original_dpi[0]

            # Only resize if downscaling (don't upscale low-res images)
            if (
                scale_factor < 1.0 and abs(scale_factor - 1.0) > 0.01
            ):  # Allow 1% tolerance
                new_width = int(image.width * scale_factor)
                new_height = int(image.height * scale_factor)
                logging.debug(
                    "Downscaling image from %dx%d to %dx%d (DPI: %d -> %d)",
                    image.width,
                    image.height,
                    new_width,
                    new_height,
                    int(original_dpi[0]),
                    target_dpi,
                )
                image = image.resize((new_width, new_height), Image.Resampling.LANCZOS)
            elif scale_factor > 1.0:
                logging.debug(
                    "Skipping upscale - original DPI %d is lower than target %d",
                    int(original_dpi[0]),
                    target_dpi,
                )
            else:
                logging.debug("Image already at target DPI, no resizing needed")

            # Convert to RGB if necessary (handle RGBA, grayscale, etc.)
            if image.mode not in ("RGB", "L"):
                logging.debug("Converting image from %s to RGB", image.mode)
                if image.mode == "RGBA":
                    # Create white background for RGBA images
                    background = Image.new("RGB", image.size, (255, 255, 255))
                    background.paste(
                        image, mask=image.split()[3] if len(image.split()) > 3 else None
                    )
                    image = background
                else:
                    image = image.convert("RGB")

            # Extract metadata
            metadata = {
                "filename": filename,
                "format": image.format or "unknown",
                "mode": image.mode,
                "size": f"{image.width}x{image.height}",
                "original_dpi": f"{int(original_dpi[0])}x{int(original_dpi[1])}",
                "processed_dpi": str(target_dpi),
            }

            # Add any additional metadata from the image
            if hasattr(image, "info"):
                for key, value in image.info.items():
                    if key not in ["dpi"] and isinstance(
                        value, (str, int, float, bool)
                    ):
                        try:
                            # Convert all values to strings for consistency
                            # Ensure key is a string
                            key_str = str(key) if not isinstance(key, str) else key
                            metadata[key_str.lower().replace(" ", "_")] = (
                                str(value) if not isinstance(value, str) else value
                            )
                        except Exception as e:
                            logging.warning(
                                "Could not process image metadata key %s: %s", key, e
                            )

            # Create DocumentPage with the processed image
            document_page = DocumentPage(
                filename=filename,
                image_content=image,
                metadata=metadata,
            )

            logging.debug("Successfully processed image: %s", filename)
            return [document_page]

        except Exception as e:
            logging.error("Error processing PIL Image object %s: %s", filename, str(e))
            raise

    # ==== BLOB STORAGE FUNCTIONALITY ====

    async def _get_blob_service_client(self) -> BlobServiceClient:
        """Get or create blob service client lazily (thread-safe singleton)."""
        if self._blob_service_client:
            return self._blob_service_client

        # Use lock to ensure only one client is created
        async with self._blob_client_lock:
            # Double-check after acquiring lock
            if not self._blob_service_client:
                if not self.data_storage_account:
                    raise ValueError("DATA_STORAGE_ACCOUNT_NAME not configured")

                account_url = (
                    f"https://{self.data_storage_account}.blob.core.windows.net"
                )
                self._blob_service_client = BlobServiceClient(
                    account_url=account_url, credential=self.credential
                )
                logging.debug("Blob service client initialized: %s", account_url)

        return self._blob_service_client

    @staticmethod
    def _return_none_on_upload_error(retry_state: RetryCallState) -> None:
        """Return None when retries are exhausted for upload."""
        return None

    @staticmethod
    def _return_none_on_download_error(retry_state: RetryCallState) -> None:
        """Return None when retries are exhausted for download."""
        return None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(0.5),
        retry=retry_if_exception_type(Exception),
        retry_error_callback=_return_none_on_download_error,
    )
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(1),
        retry=retry_if_exception_type(Exception),
    )
    async def _download_blob_with_retry(self, blob_client) -> Optional[bytes]:
        """
        Download blob content with retry logic.

        Args:
            blob_client: Azure BlobClient instance

        Returns:
            Blob content as bytes, or None if download fails after retries
        """
        try:
            download_stream = await blob_client.download_blob()
            return await download_stream.readall()
        except Exception as e:
            logging.warning(
                "Failed to download blob %s: %s",
                blob_client.blob_name,
                e,
            )
            raise

    @trace_operation("upload_page_image")
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(0.5),
        retry=retry_if_exception_type(Exception),
        retry_error_callback=_return_none_on_upload_error,
    )
    async def upload_page_image(
        self,
        page_image: Image.Image,
        page_images_container: str,
        source_blob_name: str,
        page_number: int,
        source_metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """
        Upload a page image to blob storage in the specified page images container.

        Args:
            page_image: PIL Image object for the page
            page_images_container: Target container name for page images (e.g., 'documents-page-images')
            source_blob_name: Source blob name (e.g., 'folder/file.pdf')
            page_number: Page number
            source_metadata: Metadata from source blob to inherit

        Returns:
            Full blob URL for the uploaded page image, or None if upload fails
        """
        try:
            # Validate input
            if not isinstance(page_image, Image.Image):
                logging.error(
                    "Invalid page_image type: expected PIL.Image.Image, got %s",
                    type(page_image),
                )
                return None

            # Construct blob name: preserve folder structure and add page number
            # Example: 'folder/file.pdf' -> 'folder/file/page_001.png'
            base_name = source_blob_name.rsplit(".", 1)[0]  # Remove extension
            target_blob_name = f"{base_name}/page_{page_number:03d}.png"

            # Convert image to PNG bytes
            buffer = io.BytesIO()
            page_image.save(buffer, format="PNG")
            image_bytes = buffer.getvalue()

            logging.debug(
                "Uploading page image: container=%s, blob=%s, size=%d bytes",
                page_images_container,
                target_blob_name,
                len(image_bytes),
            )

            # Get blob service client
            blob_service_client = await self._get_blob_service_client()

            # Ensure target container exists
            container_client = blob_service_client.get_container_client(
                page_images_container
            )
            try:
                await container_client.create_container()
                logging.info("Created container: %s", page_images_container)
            except Exception as e:
                # Container might already exist, which is fine
                if "ContainerAlreadyExists" not in str(e):
                    logging.debug(
                        "Container check for %s: %s", page_images_container, e
                    )

            # Get blob client for target
            blob_client = blob_service_client.get_blob_client(
                container=page_images_container, blob=target_blob_name
            )

            # Prepare metadata - inherit from source blob and add page-specific info
            upload_metadata = {}
            if source_metadata:
                # Only copy relevant metadata (exclude internal blob properties)
                for key, value in source_metadata.items():
                    if not key.startswith("blob_") and isinstance(
                        value, (str, int, float, bool)
                    ):
                        # Ensure metadata keys are valid (lowercase, no special chars)
                        clean_key = str(key).lower().replace(" ", "_").replace("-", "_")
                        upload_metadata[clean_key] = str(value)

            # Add page-specific metadata
            upload_metadata.update(
                {
                    "source_blob": source_blob_name,
                    "page_images_container": page_images_container,
                    "page_number": str(page_number),
                }
            )

            # Upload with metadata (with concurrency control)
            async with self._blob_semaphore:
                await blob_client.upload_blob(
                    image_bytes,
                    overwrite=True,
                    content_settings=ContentSettings(
                        content_type="image/png",
                        cache_control="public, max-age=31536000",  # Cache for 1 year
                    ),
                    metadata=upload_metadata,
                )

            # Construct and return full URL
            page_image_url = f"https://{self.data_storage_account}.blob.core.windows.net/{page_images_container}/{target_blob_name}"

            logging.info(
                "Successfully uploaded page image: %s (page %d)",
                target_blob_name,
                page_number,
            )

            return page_image_url

        except Exception as e:
            logging.error(
                "Failed to upload page image for %s (page %d): %s",
                source_blob_name,
                page_number,
                e,
            )
            raise  # Re-raise for tenacity to retry

    # ==== SERVICE BUS FUNCTIONALITY ====

    async def initialize_service_bus(self):
        """Initialize the Service Bus client using managed identity"""
        try:
            # Use managed identity authentication only
            fully_qualified_namespace = (
                f"{self.service_bus_namespace}.servicebus.windows.net"
            )
            self.service_bus_client = ServiceBusClient(
                fully_qualified_namespace=fully_qualified_namespace,
                credential=self.credential,
                logging_enable=True,
            )

            logging.debug(
                "Service Bus client initialized for namespace: %s",
                self.service_bus_namespace,
            )

        except Exception as e:
            logging.error("Failed to initialize Service Bus client: %s", e)
            raise

    async def start_message_consumption(self):
        """Start consuming messages from the Service Bus queue"""
        if not self.service_bus_client:
            await self.initialize_service_bus()

        if not self.service_bus_client:
            logging.error("Service Bus client failed to initialize")
            return

        try:
            async with self.service_bus_client:
                # Initialize AutoLockRenewer for long-running processing (30 minutes)
                renewer = AutoLockRenewer(max_lock_renewal_duration=1800)

                try:
                    # Get the receiver for the queue with lock renewal
                    receiver = self.service_bus_client.get_queue_receiver(
                        queue_name=self.queue_name,
                        max_wait_time=60,  # Wait up to 60 seconds for messages
                        auto_lock_renewer=renewer,
                        max_lock_renewal_duration=1800,  # 30 minutes for document processing
                    )

                    logging.info(
                        "Starting to consume messages from queue: %s", self.queue_name
                    )

                    async with receiver:
                        while not self.shutdown_event.is_set():
                            try:
                                # Configure concurrent message processing
                                max_concurrent_messages = int(
                                    os.getenv("DOCUMENT_MAX_CONCURRENT", "3")
                                )

                                # Receive messages in batches for concurrent processing
                                received_msgs = await receiver.receive_messages(
                                    max_message_count=max_concurrent_messages,
                                    max_wait_time=30,
                                )

                                if received_msgs:
                                    # Process messages concurrently using asyncio.gather
                                    tasks = [
                                        self._process_service_bus_message_safe(
                                            msg, receiver
                                        )
                                        for msg in received_msgs
                                    ]

                                    # Wait for all messages to complete (including exceptions)
                                    results = await asyncio.gather(
                                        *tasks, return_exceptions=True
                                    )

                                    # Log any exceptions from concurrent processing
                                    for idx, result in enumerate(results):
                                        if isinstance(result, Exception):
                                            logging.error(
                                                "Task %d failed with exception: %s",
                                                idx,
                                                result,
                                            )

                            except asyncio.CancelledError:
                                logging.info(
                                    "Message consumption cancelled, draining in-flight messages..."
                                )
                                break
                            except Exception as e:
                                logging.error("Error receiving messages: %s", e)
                                await asyncio.sleep(5)  # Wait before retrying

                        logging.info(
                            "Message consumption loop exited, draining receiver..."
                        )

                finally:
                    # Clean up AutoLockRenewer
                    try:
                        renewer.close(wait=True)
                        logging.debug("Closed AutoLockRenewer")
                    except Exception as e:
                        logging.debug("Error closing AutoLockRenewer: %s", e)

        except Exception as e:
            logging.error("Failed to start consuming messages: %s", e)
            raise

    async def _process_service_bus_message_safe(
        self, message: ServiceBusReceivedMessage, receiver
    ):
        """Safely process Service Bus message with delivery count tracking and retry logic."""
        try:
            await self._process_service_bus_message(message)
            # Complete the message to remove it from queue
            await receiver.complete_message(message)
            logging.info("Message %s completed successfully", message.message_id)
        except Exception as e:
            # Get delivery count (defaults to 0 if None)
            delivery_count = message.delivery_count or 0

            logging.error(
                "Error processing message %s (delivery count: %d): %s",
                message.message_id,
                delivery_count,
                e,
            )

            # Check delivery count to decide retry vs dead-letter
            if delivery_count >= 2:
                # Dead-letter after 2 failures (3rd attempt would be delivery_count=2)
                try:
                    await receiver.dead_letter_message(
                        message,
                        reason="MaxDeliveryCountExceeded",
                        error_description=f"Failed after {delivery_count} attempts. Last error: {str(e)}",
                    )
                    logging.warning(
                        "Message %s dead-lettered after %d failures",
                        message.message_id,
                        delivery_count,
                    )
                except Exception as dead_letter_error:
                    logging.error(
                        "Failed to dead-letter message %s: %s",
                        message.message_id,
                        dead_letter_error,
                    )
                    # Fallback to abandon if dead-letter fails
                    try:
                        await receiver.abandon_message(message)
                    except Exception as abandon_error:
                        logging.error(
                            "Failed to abandon message %s: %s",
                            message.message_id,
                            abandon_error,
                        )
            else:
                # Abandon for retry (delivery count will increment)
                try:
                    await receiver.abandon_message(message)
                    logging.warning(
                        "Abandoned message %s for retry (attempt %d)",
                        message.message_id,
                        delivery_count,
                    )
                except Exception as abandon_error:
                    logging.error(
                        "Failed to abandon message %s: %s",
                        message.message_id,
                        abandon_error,
                    )

    async def _process_service_bus_message(self, message: ServiceBusMessage):
        """Process a single message from Service Bus"""
        try:
            # Parse the message body (should be Event Grid event)
            message_body = str(message)
            event_data = json.loads(message_body)

            logging.info("Processing message: %s", message.message_id)
            logging.debug("Message content: %s", event_data)

            # Event Grid events come as arrays
            if isinstance(event_data, list):
                events = event_data
            else:
                events = [event_data]

            # Process each event in the message
            for event in events:
                await self._process_event(event)

        except json.JSONDecodeError as e:
            logging.error("Failed to parse message as JSON: %s", e)
            raise
        except Exception as e:
            logging.error("Error processing message: %s", e)
            raise

    async def _process_event(self, event_dict: Dict[str, Any]):
        """Process a single Event Grid event"""
        try:
            # Parse into structured model
            event = ServiceBusEvent.from_message_body(event_dict)

            logging.info("Processing event type: %s", event.event_type)

            if event.event_type == "Microsoft.Storage.BlobCreated":
                await self._handle_blob_created(event.data)
            elif event.event_type == "Microsoft.Storage.BlobDeleted":
                await self._handle_blob_deleted(event.data)
            else:
                logging.warning("Unknown event type: %s", event.event_type)

        except Exception as e:
            logging.error("Error processing event: %s", e)
            raise

    @trace_operation("handle_blob_created", new_root=True)
    async def _handle_blob_created(self, event_data: BlobEvent):
        """Handle blob created event"""
        try:
            logging.info("Processing blob created: %s", event_data.blob_url)

            # Check if file format is supported
            if event_data.file_extension not in self.supported_formats:
                logging.info(
                    "Skipping unsupported file format '%s': %s",
                    event_data.file_extension,
                    event_data.blob_url,
                )
                return

            # Parse blob URL to get container and blob name
            url_parts = event_data.blob_url.split("/")
            container_name = url_parts[3]
            blob_name = "/".join(url_parts[4:])

            logging.info("Processing document: %s/%s", container_name, blob_name)

            # Determine page images container name
            page_images_container = f"{container_name}-page-images"

            # Use shared blob service client to download blob
            blob_service_client = await self._get_blob_service_client()
            blob_client = blob_service_client.get_blob_client(
                container=container_name, blob=blob_name
            )

            # Download blob content and get blob metadata
            blob_metadata = {}

            # Get blob properties and metadata first
            blob_properties = await blob_client.get_blob_properties()

            # Extract relevant blob metadata
            blob_metadata.update(
                {
                    "blob_size_bytes": blob_properties.size,
                    "blob_content_type": blob_properties.content_settings.content_type,
                    "blob_last_modified": blob_properties.last_modified.isoformat()
                    if blob_properties.last_modified
                    else None,
                    "blob_etag": blob_properties.etag,
                    "blob_creation_time": blob_properties.creation_time.isoformat()
                    if blob_properties.creation_time
                    else None,
                }
            )

            # Add custom metadata if present
            if blob_properties.metadata:
                for key, value in blob_properties.metadata.items():
                    # Add custom metadata directly without prefix
                    blob_metadata[key.lower()] = value

            blob_content = await self._download_blob_with_retry(blob_client)
            if blob_content is None:
                logging.error("Failed to download blob after retries: %s", blob_name)
                return

            logging.info(
                "Downloaded blob: %s, size: %s bytes, metadata: %s",
                blob_name,
                len(blob_content),
                blob_metadata,
            )

            # Process the document through full pipeline
            result = await self.process_document_complete(
                blob_content=blob_content,
                blob_name=blob_name,
                file_extension=event_data.file_extension,
                blob_url=event_data.blob_url,
                page_images_container=page_images_container,
                blob_metadata=blob_metadata,
            )

            if result.success:
                logging.info(
                    "Successfully processed document: %s (%.1f%% success rate)",
                    blob_name,
                    result.success_rate,
                )
            else:
                logging.error(
                    "Failed to process document: %s - %s",
                    blob_name,
                    result.error_message,
                )

        except Exception as e:
            logging.error("Error handling blob created event: %s", e)
            raise

    @trace_operation("handle_blob_deleted", new_root=True)
    async def _handle_blob_deleted(self, event_data: BlobEvent):
        """Handle blob deleted event"""
        try:
            logging.info("Processing blob deleted: %s", event_data.blob_url)

            # Check if file format is supported
            if event_data.file_extension not in self.supported_formats:
                logging.info(
                    "Skipping unsupported file format '%s': %s",
                    event_data.file_extension,
                    event_data.blob_url,
                )
                return

            # Delete page images from blob storage and Qdrant index in parallel
            async def delete_page_images():
                """Delete page images from blob storage"""
                try:
                    # Parse blob URL to get container and blob name
                    url_parts = event_data.blob_url.split("/")
                    if len(url_parts) >= 4:
                        source_container = url_parts[3]
                        source_blob_name = "/".join(url_parts[4:])
                        target_container = f"{source_container}-page-images"

                        # Construct the folder path for page images
                        base_name = source_blob_name.rsplit(".", 1)[0]
                        page_images_prefix = f"{base_name}/"

                        # Delete all page images for this document
                        blob_service_client = await self._get_blob_service_client()
                        container_client = blob_service_client.get_container_client(
                            target_container
                        )

                        deleted_count = 0
                        async for blob in container_client.list_blobs(
                            name_starts_with=page_images_prefix
                        ):
                            try:
                                async with self._blob_semaphore:
                                    await container_client.delete_blob(blob.name)
                                deleted_count += 1
                            except Exception as e:
                                logging.warning(
                                    "Failed to delete page image %s: %s", blob.name, e
                                )

                        if deleted_count > 0:
                            logging.info(
                                "Deleted %d page images from %s",
                                deleted_count,
                                target_container,
                            )

                except Exception as e:
                    logging.warning(
                        "Failed to delete page images for %s: %s",
                        event_data.blob_url,
                        e,
                    )
                    # Don't raise - we want to continue even if blob deletion fails

            async def delete_from_qdrant():
                """Delete document pages from Qdrant index"""
                await self.qdrant_index.delete_document_pages(event_data.document_id)

            # Run both deletions in parallel
            await asyncio.gather(
                delete_page_images(),
                delete_from_qdrant(),
                return_exceptions=True,  # Continue even if one fails
            )

            logging.info(
                "Successfully deleted document from index: %s", event_data.document_id
            )

        except Exception as e:
            logging.error("Error handling blob deleted event: %s", e)
            raise

    # ==== COMPLETE PROCESSING PIPELINE ====

    @trace_operation("process_document_complete", new_root=True)
    async def process_document_complete(
        self,
        blob_content: Union[bytes, Image.Image],
        blob_name: str,
        file_extension: str,
        blob_url: Optional[str] = None,
        page_images_container: Optional[str] = None,
        blob_metadata: Optional[Dict[str, Any]] = None,
    ) -> ProcessingResult:
        """
        Complete document processing pipeline: PDF/Image -> Pages -> Embeddings -> Index

        Args:
            blob_content: Raw document content as bytes, or PIL Image object for direct image processing
            blob_name: Name of the blob/file
            file_extension: File extension (e.g., '.pdf', '.jpg', '.jpeg', '.png')
            blob_url: Optional full URL to the blob
            page_images_container: Optional target container name for page images (if not provided, derived from blob_url)
            blob_metadata: Optional metadata dictionary

        Returns:
            ProcessingResult with success status and metrics
        """
        start_time = time.time()

        try:
            logging.info("Starting complete processing pipeline for: %s", blob_name)

            # Construct blob URL if not provided (for cases where we have the blob_name but not full URL)
            if not blob_url and self.data_storage_account:
                blob_url = f"https://{self.data_storage_account}.blob.core.windows.net/documents/{blob_name}"
                logging.info("Constructed blob URL: %s", blob_url)

            # Derive page_images_container from blob_url if not provided
            if not page_images_container and blob_url:
                url_parts = blob_url.split("/")
                if len(url_parts) >= 4:
                    source_container = url_parts[3]
                    page_images_container = f"{source_container}-page-images"
                    logging.debug(
                        "Derived page_images_container from blob_url: %s",
                        page_images_container,
                    )

            # Use blob_url as document_id when available, otherwise fall back to blob_name
            document_id = blob_url if blob_url else blob_name

            # Initialize QDRANT index only once (thread-safe with async lock)
            async with self._init_lock:
                if not self._index_initialized:
                    if not await self.qdrant_index.initialize():
                        logging.error("Failed to initialize QDRANT index")
                        return ProcessingResult(
                            success=False,
                            document_id=document_id,
                            error_message="Failed to initialize QDRANT index",
                            processing_time_seconds=time.time() - start_time,
                        )
                    self._index_initialized = True
                    logging.info("QDRANT index initialized successfully")

            # Step 1: Process document into page chunks
            document_pages = self.process_document(
                content=blob_content, filename=blob_name, file_type=file_extension
            )

            logging.info("Document split into %s pages", len(document_pages))

            # Free memory (only if bytes, not PIL Image objects)
            if isinstance(blob_content, bytes):
                del blob_content

            # Process all pages concurrently with rolling pipeline
            # Each client (ColPali, Blob, Qdrant) manages its own concurrency via semaphores
            logging.info(
                "Processing %s pages with rolling pipeline (each client controls its own concurrency)",
                len(document_pages),
            )

            processed_count = await self._process_pages_rolling(
                document_pages=document_pages,
                document_id=document_id,
                blob_name=blob_name,
                file_extension=file_extension,
                blob_url=blob_url,
                page_images_container=page_images_container,
                blob_metadata=blob_metadata or {},
            )

            logging.info(
                "Complete processing finished: %s/%s pages processed successfully",
                processed_count,
                len(document_pages),
            )

            # Delete orphan pages (pages that no longer exist in document)
            if self.delete_existing_pages and processed_count > 0:
                total_pages = len(document_pages)
                orphan_delete_success = await self.qdrant_index.delete_orphan_pages(
                    document_id=document_id,
                    max_valid_page_number=total_pages,
                )
                if orphan_delete_success:
                    logging.info(
                        "Cleaned up orphan pages (page_number > %d) for document: %s",
                        total_pages,
                        blob_name,
                    )
                else:
                    logging.warning(
                        "Could not clean up orphan pages for document: %s",
                        blob_name,
                    )

            return ProcessingResult(
                success=processed_count > 0,
                document_id=document_id,
                pages_processed=processed_count,
                total_pages=len(document_pages),
                processing_time_seconds=time.time() - start_time,
                error_message=None
                if processed_count > 0
                else f"Only {processed_count}/{len(document_pages)} pages processed successfully",
            )

        except Exception as e:
            logging.error(
                "Error in complete document processing for %s: %s", blob_name, e
            )
            return ProcessingResult(
                success=False,
                document_id=document_id,
                error_message=str(e),
                processing_time_seconds=time.time() - start_time,
            )

    @trace_operation("process_pages_rolling")
    async def _process_pages_rolling(
        self,
        document_pages: List[DocumentPage],
        document_id: str,
        blob_name: str,
        file_extension: str,
        blob_url: Optional[str],
        page_images_container: Optional[str],
        blob_metadata: Dict[str, Any],
    ) -> int:
        """
        Process pages with a rolling pipeline approach.

        Each page flows through the pipeline independently:
        ColPali (embedding) -> Blob (upload) -> Qdrant (index)

        Concurrency is controlled by each client's semaphore:
        - ColPali: COLPALI_MAX_CONCURRENT
        - Blob: BLOB_MAX_CONCURRENT
        - Qdrant: QDRANT_MAX_CONCURRENT

        This allows maximum throughput as pages don't wait for batch boundaries.
        """

        async def process_single_page(document_page: DocumentPage) -> bool:
            """Process a single page through the full pipeline."""
            try:
                page_num = document_page.page_number
                logging.debug("Starting pipeline for page %d", page_num)

                # Step 1: Generate embeddings (ColPali semaphore controls concurrency)
                embeddings_response = await self.colpali_client.generate_embeddings(
                    document_page
                )

                if not embeddings_response:
                    logging.warning("No embeddings generated for page %d", page_num)
                    return False

                # Step 2: Upload page image (Blob semaphore controls concurrency)
                page_image_url = None
                if page_images_container and blob_name:
                    page_image_url = await self.upload_page_image(
                        page_image=document_page.image_content,
                        page_images_container=page_images_container,
                        source_blob_name=blob_name,
                        page_number=page_num,
                        source_metadata=blob_metadata,
                    )

                    if not page_image_url:
                        logging.warning(
                            "Failed to upload page image for page %d, continuing without image URL",
                            page_num,
                        )

                # Step 3: Create ProcessedPage and index (Qdrant semaphore controls concurrency)
                processed_page = ProcessedPage.from_document_page(
                    document_page=document_page,
                    document_id=document_id,
                    file_extension=file_extension,
                    blob_url=blob_url,
                    page_image_url=page_image_url,
                    additional_metadata=blob_metadata,
                )
                processed_page.embeddings = embeddings_response

                # Index single page
                index_success = await self.qdrant_index.index_embeddings(
                    [processed_page]
                )

                if index_success:
                    logging.debug("Successfully processed page %d", page_num)
                    return True
                else:
                    logging.warning("Failed to index page %d", page_num)
                    return False

            except Exception as e:
                logging.error(
                    "Error processing page %d: %s",
                    document_page.page_number,
                    e,
                )
                return False

        # Process all pages concurrently - each client's semaphore controls its own concurrency
        results = await asyncio.gather(
            *[process_single_page(page) for page in document_pages],
            return_exceptions=True,
        )

        # Count successful results
        processed_count = sum(1 for result in results if result is True)

        logging.info(
            "Rolling pipeline completed: %s/%s pages processed successfully",
            processed_count,
            len(document_pages),
        )

        return processed_count
