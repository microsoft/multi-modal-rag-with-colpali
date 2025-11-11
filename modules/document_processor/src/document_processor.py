# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""
Document processor for handling PDF files with Service Bus message consumption and full processing pipeline.
Combines PDF processing, Service Bus consumption, ColQwen2 embedding generation, and QDRANT indexing.
"""

import asyncio
import io
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

import fitz  # PyMuPDF
from azure.identity.aio import DefaultAzureCredential, ManagedIdentityCredential
from azure.servicebus import ServiceBusMessage
from azure.servicebus.aio import ServiceBusClient
from azure.storage.blob.aio import BlobServiceClient
from PIL import Image

from .colpali_client import ColPaliClient
from .logging import trace_operation
from .models import (
    BlobEvent,
    DocumentPage,
    ProcessedPage,
    ProcessingResult,
    ServiceBusEvent,
)
from .qdrant_index import QdrantIndex


class DocumentProcessor:
    """
    Complete document processing service with Service Bus consumption and full pipeline.
    Handles PDF processing, Service Bus messages, ColQwen2 embedding generation, and QDRANT indexing.
    """

    def __init__(self, require_service_bus: bool = True):
        # PDF processing configuration
        self.supported_formats = {".pdf"}  # Only PDF support for now
        self.pdf_image_dpi = int(os.getenv("COLPALI_IMAGE_DPI", "150"))

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

        # Initialize processing components (QDRANT and ColQwen2 client)
        self.qdrant_index = QdrantIndex(
            credential=self.credential, require_endpoint=require_service_bus
        )
        self.colpali_client = ColPaliClient(require_endpoint=require_service_bus)

        # Service Bus client (initialized later)
        self.service_bus_client = None

        logging.info("DocumentProcessor initialized - DPI: %s", self.pdf_image_dpi)
        logging.info("Service Bus namespace: %s", self.service_bus_namespace)
        logging.info("Service Bus queue: %s", self.queue_name)

    @trace_operation("process_document")
    def process_document(
        self, content: bytes, filename: str, file_type: str
    ) -> List[DocumentPage]:
        """
        Process a document and split it into chunks/pages.

        Args:
            content: Raw document content as bytes
            filename: Name of the file
            file_type: File extension (e.g., '.pdf', '.docx')

        Returns:
            List of document chunks with metadata
        """
        logging.info("Processing %s document: %s", file_type, filename)

        if file_type not in self.supported_formats:
            raise ValueError(f"Unsupported file format: {file_type}")

        pages = []

        try:
            if file_type == ".pdf":
                pages = self._process_pdf(content, filename)
            else:
                raise ValueError(f"Only PDF files are supported, got: {file_type}")

        except Exception as e:
            logging.error("Error processing document %s: %s", filename, str(e))
            raise

        logging.info("Successfully processed %s into %s pages", filename, len(pages))
        return pages

    @trace_operation("process_pdf")
    def _process_pdf(self, content: bytes, filename: str) -> List[DocumentPage]:
        """Process PDF document into page chunks."""
        pages = []

        # Use PyMuPDF for better image extraction and text handling
        pdf_document = fitz.open(stream=content, filetype="pdf")

        # Process all pages - no artificial limits
        total_pages = len(pdf_document)
        logging.info("Processing PDF with %s pages", total_pages)

        for page_num in range(total_pages):
            page = pdf_document.load_page(page_num)

            # Extract text from page
            text = str(page.get_text())

            # Render page as image for ColQwen2 processing using configured DPI
            zoom_factor = self.pdf_image_dpi / 72.0  # 72 DPI is default
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom_factor, zoom_factor))
            page_image = Image.open(io.BytesIO(pix.tobytes("png")))

            # Create DocumentPage model with single image
            document_page = DocumentPage(
                page_number=page_num + 1,
                text_content=text,
                image_content=page_image,
            )

            pages.append(document_page)

        pdf_document.close()
        return pages

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

            logging.info(
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
                # Get the receiver for the queue
                receiver = self.service_bus_client.get_queue_receiver(
                    queue_name=self.queue_name,
                    max_wait_time=60,  # Wait up to 60 seconds for messages
                )

                logging.info(
                    "Starting to consume messages from queue: %s", self.queue_name
                )

                async with receiver:
                    while True:
                        try:
                            # Receive messages (batch of 1 for sequential processing)
                            received_msgs = await receiver.receive_messages(
                                max_message_count=1, max_wait_time=30
                            )

                            for msg in received_msgs:
                                try:
                                    await self._process_service_bus_message(msg)
                                    # Complete the message to remove it from queue
                                    await receiver.complete_message(msg)
                                    logging.info(
                                        "Message %s completed successfully",
                                        msg.message_id,
                                    )

                                except Exception as e:
                                    logging.error(
                                        "Error processing message %s: %s",
                                        msg.message_id,
                                        e,
                                    )
                                    # Dead letter the message after max retries
                                    await receiver.dead_letter_message(
                                        msg,
                                        reason="ProcessingError",
                                        error_description=str(e),
                                    )

                        except Exception as e:
                            logging.error("Error receiving messages: %s", e)
                            await asyncio.sleep(5)  # Wait before retrying

        except Exception as e:
            logging.error("Failed to start consuming messages: %s", e)
            raise

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

            # Only process PDF files
            if not event_data.is_pdf:
                logging.info("Skipping non-PDF file: %s", event_data.blob_url)
                return

            # Parse blob URL to get container and blob name
            url_parts = event_data.blob_url.split("/")
            container_name = url_parts[3]
            blob_name = "/".join(url_parts[4:])

            # Skip if not in documents container
            if container_name != "documents":
                logging.info("Skipping blob in container: %s", container_name)
                return

            logging.info("Processing document: %s/%s", container_name, blob_name)

            # Download and process the document
            storage_account_name = os.getenv("DATA_STORAGE_ACCOUNT_NAME")
            if not storage_account_name:
                logging.error("DATA_STORAGE_ACCOUNT_NAME not configured")
                return

            # Initialize blob service client
            blob_service_client = BlobServiceClient(
                account_url=f"https://{storage_account_name}.blob.core.windows.net",
                credential=self.credential,
            )

            # Download blob content
            async with blob_service_client:
                blob_client = blob_service_client.get_blob_client(
                    container=container_name, blob=blob_name
                )

                download_stream = await blob_client.download_blob()
                blob_content = await download_stream.readall()

            logging.info(
                "Downloaded blob: %s, size: %s bytes", blob_name, len(blob_content)
            )

            # Process the document through full pipeline
            result = await self.process_document_complete(
                blob_content=blob_content,
                blob_name=blob_name,
                file_extension=".pdf",
                blob_url=event_data.blob_url,
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

            # Only process PDF files
            if not event_data.is_pdf:
                logging.info("Skipping non-PDF file: %s", event_data.blob_url)
                return

            # Delete from search index
            await self.qdrant_index.delete_document_pages(event_data.document_id)

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
        blob_content: bytes,
        blob_name: str,
        file_extension: str,
        blob_url: Optional[str] = None,
    ) -> ProcessingResult:
        """
        Complete document processing pipeline: PDF -> Pages -> Embeddings -> Index
        """
        start_time = time.time()
        document_id = blob_name.replace(".pdf", "")

        try:
            logging.info("Starting complete processing pipeline for: %s", blob_name)

            # Construct blob URL if not provided (for cases where we have the blob_name but not full URL)
            if not blob_url and self.data_storage_account:
                blob_url = f"https://{self.data_storage_account}.blob.core.windows.net/documents/{blob_name}"
                logging.info("Constructed blob URL: %s", blob_url)

            # Initialize QDRANT index if not already done
            if not await self.qdrant_index.initialize():
                logging.error("Failed to initialize QDRANT index")
                return ProcessingResult(
                    success=False,
                    document_id=document_id,
                    error_message="Failed to initialize QDRANT index",
                    processing_time_seconds=time.time() - start_time,
                )

            # Delete existing pages for this document to handle re-indexing scenarios
            delete_success = await self.qdrant_index.delete_document_pages(blob_name)
            if delete_success:
                logging.info("Cleared existing pages for document: %s", blob_name)
            else:
                logging.warning(
                    "Could not clear existing pages for document: %s, continuing anyway",
                    blob_name,
                )

            # Step 1: Process document into page chunks
            document_pages = self.process_document(
                content=blob_content, filename=blob_name, file_type=file_extension
            )

            logging.info("PDF split into %s pages", len(document_pages))

            # Free memory
            del blob_content

            # Step 2: Process pages sequentially for reliable processing
            processed_count = 0
            document_id = blob_name.replace(".pdf", "")

            for i, document_page in enumerate(document_pages):
                try:
                    logging.info("Processing page %s of %s", i + 1, len(document_pages))

                    # Create ProcessedPage from DocumentPage
                    processed_page = ProcessedPage.from_document_page(
                        document_page=document_page,
                        document_id=document_id,
                        filename=blob_name,
                        file_extension=file_extension,
                        blob_url=blob_url,
                    )

                    # Step 3: Generate embeddings using ColQwen2
                    embeddings_response = await self.colpali_client.generate_embeddings(
                        document_page
                    )

                    if embeddings_response:
                        # Store embeddings directly in ProcessedPage
                        processed_page.embeddings = embeddings_response

                        # Step 4: Index in QDRANT
                        index_success = await self.qdrant_index.index_embeddings(
                            [processed_page]
                        )

                        if index_success:
                            processed_count += 1
                            logging.info(
                                "Successfully processed and indexed page %s",
                                i + 1,
                            )
                        else:
                            logging.warning("Failed to index page %s", i + 1)
                    else:
                        logging.warning("No embeddings generated for page %s", i + 1)

                except Exception as e:
                    logging.error("Error processing page %s: %s", i + 1, e)
                    continue

            logging.info(
                "Complete processing finished: %s/%s pages processed successfully",
                processed_count,
                len(document_pages),
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
