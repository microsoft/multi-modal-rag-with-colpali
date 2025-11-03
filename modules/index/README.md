# QDRANT Index Module

Creates and manages QDRANT indexes for ColPali document embeddings.

## What It Does

Deploys search indexes that store:
- Document metadata (file names, page numbers, timestamps)
- Base64-encoded page images
- Multi-vector ColPali embeddings (original + mean pooled variants)
- Optimized vector search configurations for similarity search

## Available Scripts

### QDRANT Index (`deploy_qdrant.py`)
- Full multi-vector configuration with MAX_SIM comparator
- All embedding variants stored as searchable vectors
- Requires QDRANT endpoint from container apps deployment

## Configuration

Scripts read configuration from environment variables:
- `QDRANT_ENDPOINT` - QDRANT service URL (from container apps)
- `LOG_LEVEL` - Optional logging level (default: INFO)

Collection name fixed as `colpali-documents`.
