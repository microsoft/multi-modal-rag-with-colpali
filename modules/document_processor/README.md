# Document Processor Service

This module provides a containerized document processing pipeline that converts PDF documents into ColQwen2 embeddings for visual document search with QDRANT vector database integration.

## Architecture Overview

This service uses a **Service Bus consumer pattern** for reliable document processing:

1. **Service Bus Consumer** - Listens for document events with guaranteed delivery and retry mechanisms
2. **PDF Processing** - Converts multi-page PDFs into individual page images with configurable DPI
3. **ColQwen2 Integration** - Sends page images to ColQwen2 inference service for visual embeddings
4. **QDRANT Indexing** - Stores embeddings in QDRANT vector database for efficient similarity search
5. **Document Lifecycle** - Handles both document creation and deletion events

## Key Components

- `src/document_processor.py` - Main processing pipeline with Service Bus integration
- `src/colpali_client.py` - ColQwen2 service client with async request handling
- `src/qdrant_index.py` - QDRANT vector database operations and indexing
- `src/models.py` - Pydantic models for document processing pipeline
- `src/app.py` - Service Bus consumer with health endpoints
- `src/logging.py` - Telemetry and distributed tracing integration

## Processing Pipeline

```yaml
Document Processing Flow:
  Event Trigger: Azure Storage blob events via Service Bus
  PDF Conversion: Multi-page splitting with PIL/Poppler
  Embedding Generation: ColQwen2 visual understanding model
  Vector Storage: QDRANT collection with metadata
  Search Integration: Semantic similarity retrieval
```
