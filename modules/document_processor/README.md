# Document Processor Service

A consolidated document processing service that handles PDF documents via Azure Service Bus message queue and creates ColPali embeddings for visual document search. This provides reliable 1-in-1-out processing with guaranteed message delivery using managed identity authentication.

## What it does

1. **Consumes messages**: Listens to Azure Service Bus queue for document processing events from Event Grid
2. **Converts to images**: Splits PDFs into page images (configurable DPI and page limits)
3. **Creates embeddings**: Sends images to ColPali/ColQwen2 endpoint to generate visual embeddings
4. **Stores in search**: Indexes the embeddings in QDRANT vector database for retrieval
5. **Handles deletions**: Removes document pages from the index when files are deleted from storage

## Architecture

Event Grid → Service Bus Queue → Document Processor → QDRANT Index

This provides several benefits:
- **Sequential processing**: Messages are processed one at a time in order
- **Reliable delivery**: Built-in retry mechanisms and dead letter queues
- **Load leveling**: Queue acts as a buffer during traffic spikes
- **Message durability**: Messages are persisted until successfully processed
- **Managed identity**: Secure authentication without connection strings

## Key components

- `document_processor.py` - Main service with integrated Service Bus consumption and PDF processing
- `colpali_client.py` - ColPali/ColQwen2 ML endpoint client for embedding generation
- `qdrant_index.py` - QDRANT vector database client for indexing
- `models.py` - Data models for the processing pipeline
- `app.py` - FastAPI application with health endpoints

## Configuration

The service requires the following environment variables and uses managed identity for authentication:

### Required Environment Variables

```bash
# Service Bus Configuration (Managed Identity)
SERVICE_BUS_NAMESPACE_NAME=your-servicebus-namespace  # Required - namespace only, no connection string
SERVICE_BUS_QUEUE_NAME=document-processing       # Optional - defaults to "document-processing"

# Storage Configuration
DATA_STORAGE_ACCOUNT_NAME=your-storage-account   # Required - for blob access

# Vector Database
QDRANT_ENDPOINT=https://your-qdrant-endpoint  # Required - QDRANT vector database

# ML Endpoint
AML_EMBEDDING_ENDPOINT_URL=https://your-colpali-endpoint/score  # Required - ColPali/ColQwen2 endpoint

# Processing Settings (Optional)
COLPALI_IMAGE_DPI=150  # Optional - PDF to image conversion DPI, defaults to 150

# Azure Authentication (Optional)
AZURE_CLIENT_ID=your-managed-identity-client-id  # Optional - for specific managed identity
```

### Authentication

The service uses Azure Managed Identity for authentication to Azure services. The service will refuse to start if any required environment variables are missing.## Local Development

To test locally:

```bash
# Install dependencies
cd modules/document_processor
python -m venv .venv
.\.venv\Scripts\Activate.ps1  # Windows
pip install -e .

# Set environment variables
# Export required environment variables listed above

# Run test
python scripts/test_local.py
```
