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
from typing import Any, Dict, List

import fitz  # PyMuPDF
from azure.identity.aio import DefaultAzureCredential, ManagedIdentityCredential
from azure.servicebus import ServiceBusMessage
from azure.servicebus.aio import ServiceBusClient
from azure.storage.blob.aio import BlobServiceClient

# Import OpenTelemetry tracing
from PIL import Image

from .colpali_client import ColPaliClient
from .logging import trace_operation
from .models import (
    BlobEvent,
    DocumentPage,
    EmbeddingData,
    ProcessedPage,
    ProcessingResult,
    ServiceBusEvent,
)
from .qdrant_index import QdrantIndex


class DocumentProcessor:
    """
    Complete document processing service with Service Bus consumption and full pipeline.
    Handles PDF processing, Service Bus messages, ColPali embedding generation, and QDRANT indexing.
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

        # Initialize processing components
        self.qdrant_index = QdrantIndex(
            credential=self.credential, require_endpoint=require_service_bus
        )
        self.colpali_client = ColPaliClient(require_endpoint=require_service_bus)

        # Service Bus client (initialized later)
        self.service_bus_client = None

        logging.info(f"DocumentProcessor initialized - DPI: {self.pdf_image_dpi}")
        logging.info(f"Service Bus namespace: {self.service_bus_namespace}")
        logging.info(f"Service Bus queue: {self.queue_name}")

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

    @trace_operation("process_pdf")
    def _process_pdf(self, content: bytes, filename: str) -> List[DocumentPage]:
        """Process PDF document into page chunks."""
        pages = []

        # Use PyMuPDF for better image extraction and text handling
        pdf_document = fitz.open(stream=content, filetype="pdf")

        # Process all pages - no artificial limits
        total_pages = len(pdf_document)
        logging.info(f"Processing PDF with {total_pages} pages")

        for page_num in range(total_pages):
            page = pdf_document.load_page(page_num)

            # Extract text from page
            text = str(page.get_text())

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
                    page_images.append(image)
                except Exception as e:
                    logging.warning(
                        f"Could not extract image {img_index} from page {page_num}: {e}"
                    )

            # Render page as image for ColPali processing using configured DPI
            zoom_factor = self.pdf_image_dpi / 72.0  # 72 DPI is default
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom_factor, zoom_factor))
            page_image = Image.open(io.BytesIO(pix.tobytes("png")))

            # Create DocumentPage model
            all_images = [page_image] + page_images  # Type will be inferred correctly
            document_page = DocumentPage(
                page_number=page_num + 1,
                text_content=text,
                images=all_images,  # Main page image + extracted images
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
                f"Service Bus client initialized for namespace: {self.service_bus_namespace}"
            )

        except Exception as e:
            logging.error(f"Failed to initialize Service Bus client: {e}")
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
                    f"Starting to consume messages from queue: {self.queue_name}"
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
                                        f"Message {msg.message_id} completed successfully"
                                    )

                                except Exception as e:
                                    logging.error(
                                        f"Error processing message {msg.message_id}: {e}"
                                    )
                                    # Dead letter the message after max retries
                                    await receiver.dead_letter_message(
                                        msg,
                                        reason="ProcessingError",
                                        error_description=str(e),
                                    )

                        except Exception as e:
                            logging.error(f"Error receiving messages: {e}")
                            await asyncio.sleep(5)  # Wait before retrying

        except Exception as e:
            logging.error(f"Failed to start consuming messages: {e}")
            raise

    async def _process_service_bus_message(self, message: ServiceBusMessage):
        """Process a single message from Service Bus"""
        try:
            # Parse the message body (should be Event Grid event)
            message_body = str(message)
            event_data = json.loads(message_body)

            logging.info(f"Processing message: {message.message_id}")
            logging.debug(f"Message content: {event_data}")

            # Event Grid events come as arrays
            if isinstance(event_data, list):
                events = event_data
            else:
                events = [event_data]

            # Process each event in the message
            for event in events:
                await self._process_event(event)

        except json.JSONDecodeError as e:
            logging.error(f"Failed to parse message as JSON: {e}")
            raise
        except Exception as e:
            logging.error(f"Error processing message: {e}")
            raise

    async def _process_event(self, event_dict: Dict[str, Any]):
        """Process a single Event Grid event"""
        try:
            # Parse into structured model
            event = ServiceBusEvent.from_message_body(event_dict)

            logging.info(f"Processing event type: {event.event_type}")

            if event.event_type == "Microsoft.Storage.BlobCreated":
                await self._handle_blob_created(event.data)
            elif event.event_type == "Microsoft.Storage.BlobDeleted":
                await self._handle_blob_deleted(event.data)
            else:
                logging.warning(f"Unknown event type: {event.event_type}")

        except Exception as e:
            logging.error(f"Error processing event: {e}")
            raise

    @trace_operation("handle_blob_created", new_root=True)
    async def _handle_blob_created(self, event_data: BlobEvent):
        """Handle blob created event"""
        try:
            logging.info(f"Processing blob created: {event_data.blob_url}")

            # Only process PDF files
            if not event_data.is_pdf:
                logging.info(f"Skipping non-PDF file: {event_data.blob_url}")
                return

            # Parse blob URL to get container and blob name
            url_parts = event_data.blob_url.split("/")
            container_name = url_parts[3]
            blob_name = "/".join(url_parts[4:])

            # Skip if not in documents container
            if container_name != "documents":
                logging.info(f"Skipping blob in container: {container_name}")
                return

            logging.info(f"Processing document: {container_name}/{blob_name}")

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
                f"Downloaded blob: {blob_name}, size: {len(blob_content)} bytes"
            )

            # Process the document through full pipeline
            result = await self.process_document_complete(
                blob_content=blob_content, blob_name=blob_name, file_extension=".pdf"
            )

            if result.success:
                logging.info(
                    f"Successfully processed document: {blob_name} ({result.success_rate:.1f}% success rate)"
                )
            else:
                logging.error(
                    f"Failed to process document: {blob_name} - {result.error_message}"
                )

        except Exception as e:
            logging.error(f"Error handling blob created event: {e}")
            raise

    @trace_operation("handle_blob_deleted", new_root=True)
    async def _handle_blob_deleted(self, event_data: BlobEvent):
        """Handle blob deleted event"""
        try:
            logging.info(f"Processing blob deleted: {event_data.blob_url}")

            # Only process PDF files
            if not event_data.is_pdf:
                logging.info(f"Skipping non-PDF file: {event_data.blob_url}")
                return

            # Delete from search index
            await self.qdrant_index.delete_document_pages(event_data.document_id)

            logging.info(
                f"Successfully deleted document from index: {event_data.document_id}"
            )

        except Exception as e:
            logging.error(f"Error handling blob deleted event: {e}")
            raise

    # ==== COMPLETE PROCESSING PIPELINE ====

    @trace_operation("process_document_complete", new_root=True)
    async def process_document_complete(
        self, blob_content: bytes, blob_name: str, file_extension: str
    ) -> ProcessingResult:
        """
        Complete document processing pipeline: PDF -> Pages -> Embeddings -> Index
        """
        start_time = time.time()
        document_id = blob_name.replace(".pdf", "")

        try:
            logging.info(f"Starting complete processing pipeline for: {blob_name}")

            # Initialize qdrant index if not already done
            if not await self.qdrant_index.initialize():
                logging.error("Failed to initialize qdrant index")
                return ProcessingResult(
                    success=False,
                    document_id=document_id,
                    error_message="Failed to initialize qdrant index",
                    processing_time_seconds=time.time() - start_time,
                )

            # Delete existing pages for this document to handle re-indexing scenarios
            delete_success = await self.qdrant_index.delete_document_pages(blob_name)
            if delete_success:
                logging.info(f"Cleared existing pages for document: {blob_name}")
            else:
                logging.warning(
                    f"Could not clear existing pages for document: {blob_name}, continuing anyway"
                )

            # Step 1: Process document into page chunks
            document_pages = self.process_document(
                content=blob_content, filename=blob_name, file_type=file_extension
            )

            logging.info(f"PDF split into {len(document_pages)} pages")

            # Free memory
            del blob_content

            # Step 2: Process pages sequentially for reliable processing
            processed_count = 0
            document_id = blob_name.replace(".pdf", "")

            for i, document_page in enumerate(document_pages):
                try:
                    logging.info(f"Processing page {i + 1} of {len(document_pages)}")

                    # Step 3: Generate embeddings using ColPali
                    embeddings = await self.colpali_client.generate_embeddings(
                        document_page
                    )

                    # Clear page image from memory after processing
                    if document_page.images:
                        for img in document_page.images:
                            try:
                                if hasattr(img, "close") and callable(
                                    getattr(img, "close", None)
                                ):
                                    img.close()  # type: ignore[attr-defined]
                            except Exception:
                                pass  # Ignore errors when closing images

                    if embeddings:
                        # Handle new multi-embedding format
                        embeddings_dict = embeddings.get("embeddings", {})
                        patch_count = embeddings.get("patch_count", 0)

                        # Calculate dimensions from first available embedding type
                        patch_dimensions = 0
                        if embeddings_dict:
                            first_emb_type = next(iter(embeddings_dict))
                            first_embeddings = embeddings_dict[first_emb_type]
                            if first_embeddings and len(first_embeddings) > 0:
                                patch_dimensions = len(first_embeddings[0])

                        embedding_data = EmbeddingData(
                            embeddings=embeddings_dict,  # Now a dictionary of embedding types
                            num_patches=patch_count,
                            patch_dimensions=patch_dimensions,
                        )

                        # Create ProcessedPage model
                        processed_page = ProcessedPage(
                            document_id=document_id,
                            page_number=document_page.page_number,
                            text_content=document_page.text_content,
                            images=document_page.images,
                            embeddings=embedding_data,
                            source_file=blob_name,
                        )

                        # Step 4: Index in QDRANT
                        index_success = await self.qdrant_index.index_embeddings(
                            [processed_page]
                        )
                        if index_success:
                            processed_count += 1
                            logging.info(
                                f"Successfully processed and indexed page {i + 1}: {embedding_data.num_patches} patches, {embedding_data.patch_dimensions} dimensions"
                            )
                        else:
                            logging.warning(f"Failed to index page {i + 1}")
                    else:
                        logging.warning(f"No embeddings generated for page {i + 1}")

                except Exception as e:
                    logging.error(f"Error processing page {i + 1}: {e}")
                    continue

            logging.info(
                f"Complete processing finished: {processed_count}/{len(document_pages)} pages processed successfully"
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
            logging.error(f"Error in complete document processing for {blob_name}: {e}")
            return ProcessingResult(
                success=False,
                document_id=document_id,
                error_message=str(e),
                processing_time_seconds=time.time() - start_time,
            )
